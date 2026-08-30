"""Interfaces that hold the system's invariants (design section 3, 10).

These are ``typing.Protocol`` classes: structural, no inheritance required, and
checkable with mypy. The important one is ``Strategy`` - notice it has no method
that returns an ``Order`` and receives only a ``ReadOnlyContext``. A strategy
*cannot* place a trade; it can only emit ``Signal`` intent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from core.events import Fill, MarketData, Order, Signal
from core.gate_result import GateResult


@runtime_checkable
class ReadOnlyContext(Protocol):
    """What a strategy is allowed to see. No mutating methods, no broker handle."""

    @property
    def equity(self) -> float: ...

    @property
    def now(self) -> datetime: ...

    def position(self, symbol: str) -> float:
        """Signed position size in units, 0 if flat."""

    def avg_price(self, symbol: str) -> float | None: ...

    def unrealised_pnl(self, symbol: str) -> float: ...

    def history(self, symbol: str, lookback: int) -> list[MarketData]:
        """Most recent ``lookback`` bars for ``symbol``, oldest first."""


@runtime_checkable
class Strategy(Protocol):
    strategy_id: str

    def on_market_data(
        self, event: MarketData, ctx: ReadOnlyContext
    ) -> list[Signal]:
        """React to a bar/tick. Return zero or more signals. Pure given a seed."""

    def on_fill(self, event: Fill) -> None:
        """Notification only. The strategy may update internal bookkeeping; it
        has no way to act on this beyond emitting signals on the next bar."""


@runtime_checkable
class FeedAdapter(Protocol):
    """Normalises one venue into ``MarketData`` events. One adapter per venue."""

    venue: str

    async def backfill(
        self, symbol: str, start: datetime, end: datetime, interval: str
    ) -> list[MarketData]:
        """Historical bars via REST. Used to build the lake and repair gaps."""

    async def stream(self, symbols: list[str]) -> AsyncIterator[MarketData]:
        """Live quotes/bars via WebSocket."""


@runtime_checkable
class BrokerAdapter(Protocol):
    """The only component that holds broker credentials (design 4.5)."""

    broker: str

    async def submit(self, order: Order) -> str:
        """Send an order. Returns the broker's order id. Idempotent on
        ``order.client_order_id``."""

    async def cancel(self, client_order_id: str) -> None: ...

    async def positions(self) -> dict[str, float]:
        """Broker's authoritative positions. The source of truth for reconcile."""

    async def account(self) -> dict[str, float]:
        """equity, cash, margin_used, margin_available, ..."""

    async def stream_fills(self) -> AsyncIterator[Fill]: ...


@runtime_checkable
class RiskGate(Protocol):
    """One pre-trade check. The risk manager runs a list of these fail-fast."""

    code: str  # rejection code emitted on failure

    def check(self, signal: Signal, ctx: ReadOnlyContext) -> GateResult: ...


from collections.abc import AsyncIterator  # noqa: E402,F401
