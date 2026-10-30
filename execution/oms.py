"""Order Management System (design 4.5).

SCAFFOLD - build order step 6. The state machine, idempotency, and retry
classification are implemented; the broker I/O is delegated to a BrokerAdapter
and the persistence hook is a no-op until execution/persist.py is wired.

Owns the order lifecycle. The only component (with the broker adapter) that
holds credentials in production.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum

from core.bus import EventBus
from core.clock import Clock
from core.events import Fill, Order, Rejection
from core.protocols import BrokerAdapter


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


_TERMINAL = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED}

_ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.EXPIRED},
    OrderStatus.SUBMITTED: {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.REJECTED,
                            OrderStatus.CANCELLED, OrderStatus.EXPIRED},
    OrderStatus.PARTIAL: {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELLED,
                          OrderStatus.EXPIRED},
}

# broker error classification
RETRYABLE = {"timeout", "rate_limit", "5xx", "connection_reset"}
TERMINAL_ERR = {"insufficient_funds", "invalid_symbol", "market_closed", "duplicate_rejected",
                "no_position", "unsupported"}


@dataclass(slots=True)
class OrderState:
    order: Order
    status: OrderStatus = OrderStatus.PENDING
    broker_order_id: str | None = None
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    attempts: int = 0
    last_error: str = ""
    history: list[tuple[datetime, OrderStatus]] = field(default_factory=list)


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 30.0

    def delay(self, attempt: int) -> float:
        raw = min(self.max_delay, self.base_delay * 2**attempt)
        return raw * (0.5 + random.random() / 2)  # jitter


class OMS:
    def __init__(
        self,
        bus: EventBus,
        broker: BrokerAdapter,
        clock: Clock,
        *,
        retry: RetryPolicy | None = None,
        close_retry: RetryPolicy | None = None,
        persist: Callable[[OrderState], None] | None = None,
    ) -> None:
        self.bus = bus
        self.broker = broker
        self.clock = clock
        self.retry = retry or RetryPolicy()
        # A close that cannot happen YET (e.g. the broker will not amend a leg until the next
        # session opens) must wait, not be abandoned after a minute: the position stays
        # protected meanwhile. About 10 hours of patient retries by default.
        self.close_retry = close_retry or RetryPolicy(max_attempts=60, base_delay=60.0,
                                                      max_delay=900.0)
        self._tasks: set[asyncio.Task] = set()
        self.external_fills = 0  # fills for orders we did not place in this process
        self._persist = persist or (lambda _s: None)
        self._orders: dict[str, OrderState] = {}
        bus.subscribe(Order, self._on_order)

    # -- lifecycle ------------------------------------------------- #
    async def _on_order(self, order: Order) -> None:
        coid = order.client_order_id
        if coid in self._orders:  # idempotency: dedupe on client_order_id
            return
        st = OrderState(order=order)
        self._orders[coid] = st
        self._transition(st, OrderStatus.PENDING)
        if order.reduce_only:
            # Hours-long retries must not run inside the bus handler: that would stall market
            # data and every other event until the close finally goes through.
            task = asyncio.create_task(self._submit_with_retry(st, self.close_retry))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            await self._submit_with_retry(st)

    async def _submit_with_retry(self, st: OrderState, policy: RetryPolicy | None = None) -> None:
        policy = policy or self.retry
        while st.attempts < policy.max_attempts and st.status not in _TERMINAL:
            st.attempts += 1
            try:
                broker_id = await self.broker.submit(st.order)  # idempotent on coid
                st.broker_order_id = broker_id
                self._transition(st, OrderStatus.SUBMITTED)
                return
            except Exception as exc:  # noqa: BLE001
                err = _classify(exc)
                st.last_error = err
                if err in TERMINAL_ERR:
                    self._transition(st, OrderStatus.REJECTED)
                    await self.bus.publish(
                        Rejection(
                            ts=self.clock.now(),
                            reason_code=err.upper(),
                            origin="broker",
                            originating_event_id=st.order.event_id,
                            detail=str(exc),
                            symbol=st.order.symbol,
                            strategy_id=st.order.strategy_id,
                        )
                    )
                    return
                await self.clock.sleep(policy.delay(st.attempts))
        if st.status not in _TERMINAL:
            self._transition(st, OrderStatus.REJECTED)
            # never fail silently: an order that ran out of retries must reach the alerts
            await self.bus.publish(
                Rejection(
                    ts=self.clock.now(),
                    reason_code="RETRIES_EXHAUSTED",
                    origin="broker",
                    originating_event_id=st.order.event_id,
                    detail=f"gave up after {st.attempts} attempts; last error: {st.last_error}",
                    symbol=st.order.symbol,
                    strategy_id=st.order.strategy_id,
                )
            )

    async def on_broker_fill(self, fill: Fill) -> None:
        st = self._orders.get(fill.order_id)
        if st is None:
            # A fill for an order this process does not know: one placed by hand, an OCO restored
            # after an incident, or an order from before a restart. It still changed the position
            # AT THE BROKER, so the portfolio must see it; dropping it made every such fill show
            # up as false "reconcile drift" (214 critical alerts and a halt on 29-30 Oct).
            self.external_fills += 1
            await self.bus.publish(fill)
            return
        if fill.strategy_id != st.order.strategy_id:
            fill = replace(fill, strategy_id=st.order.strategy_id)
        if fill.side is not st.order.side:
            # Bracket exit (stop-loss / take-profit share the entry's order ref).
            # It reduces the position but is not part of the entry order's fills.
            await self.bus.publish(fill)
            return
        st.filled_qty += fill.qty
        total = st.filled_qty
        st.avg_fill_price = (
            (st.avg_fill_price * (total - fill.qty) + fill.price * fill.qty) / total
            if total > 0
            else fill.price
        )
        if st.filled_qty + 1e-9 >= st.order.qty:
            self._transition(st, OrderStatus.FILLED)
        else:
            self._transition(st, OrderStatus.PARTIAL)
        await self.bus.publish(fill)

    def _transition(self, st: OrderState, new: OrderStatus) -> None:
        if st.history and new not in _ALLOWED.get(st.status, set()) and new != st.status:
            raise ValueError(f"illegal OMS transition {st.status} -> {new} for {st.order.client_order_id}")
        st.status = new
        st.history.append((self.clock.now(), new))
        self._persist(st)  # write state BEFORE the next action (design 4.5)

    # -- queries ------------------------------------------------- #
    def state(self, client_order_id: str) -> OrderState | None:
        return self._orders.get(client_order_id)

    def open_orders(self) -> list[OrderState]:
        return [s for s in self._orders.values() if s.status not in _TERMINAL]


def _classify(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in name or "timeout" in msg:
        return "timeout"
    for key in RETRYABLE | TERMINAL_ERR:
        if key.replace("_", " ") in msg or key in msg:
            return key
    return "timeout"  # default: assume retryable
