"""The six event types that cover the whole execution plane (design section 3).

All events are frozen dataclasses. They are values, not objects with behaviour.
Every event carries:

* ``event_id``      - a stable UUID, used for audit correlation.
* ``ts``            - the *venue* timestamp (tz-aware UTC). Backtests replay on this.
* ``received_ts``   - the local receipt timestamp. ``ts`` == ``received_ts`` in sim.

Critical invariant: a ``Signal`` is not an ``Order``. Strategies emit intent
(``Signal``); only the risk manager may construct an ``Order``. That is enforced
here in ``Order.__post_init__`` (an order with no bracket is rejected at
construction) and by the ``Strategy`` protocol, which has no method that returns
an order.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


def _require_utc(ts: datetime, field_name: str) -> None:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC, got naive {ts!r}")
    if ts.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC, got offset {ts.utcoffset()}")


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Direction(str, Enum):
    """A strategy's directional intent. Not a side - risk decides the side."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"  # close//reduce existing exposure


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    GTD = "gtd"  # good-till-date, uses Order.ttl


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    ts: datetime
    event_id: str = field(default_factory=_new_id)
    received_ts: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        _require_utc(self.ts, f"{type(self).__name__}.ts")


# --------------------------------------------------------------------------- #
# 1. MarketData  -  Feed adapter -> Strategy engine, Portfolio
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True, kw_only=True)
class MarketData(Event):
    symbol: str
    source: str
    bid: float | None = None
    ask: float | None = None
    # OHLCV bar fields (daily / hourly). None for pure quote ticks.
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    bar_interval: str | None = None  # e.g. "1d", "1h"

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return self.close

    @property
    def spread(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return self.ask - self.bid
        return None

    @property
    def price(self) -> float:
        """Best available reference price."""
        p = self.mid if self.mid is not None else self.close
        if p is None:
            raise ValueError(f"MarketData for {self.symbol} has no usable price")
        return p


# --------------------------------------------------------------------------- #
# 2. Signal  -  Strategy -> Risk manager
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True, kw_only=True)
class Signal(Event):
    symbol: str
    direction: Direction
    strategy_id: str
    conviction: float = 1.0  # in [0, 1]; NOT a position size
    features_hash: str = ""  # version hash of the feature code that produced inputs
    # Strategy's suggested risk geometry, in price terms. Risk may override.
    entry_hint: float | None = None
    stop_hint: float | None = None
    target_hint: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Event.__post_init__(self)  # not super(): zero-arg super breaks under slots=True on 3.12
        if not 0.0 <= self.conviction <= 1.0:
            raise ValueError(f"conviction must be in [0, 1], got {self.conviction}")


# --------------------------------------------------------------------------- #
# 3. Order  -  Risk manager -> OMS   (only the risk manager builds these)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True, kw_only=True)
class Order(Event):
    client_order_id: str
    symbol: str
    side: Side
    qty: float
    order_type: OrderType
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    tif: TimeInForce = TimeInForce.DAY
    ttl: datetime | None = None
    strategy_id: str = ""
    signal_id: str = ""
    features_hash: str = ""

    def __post_init__(self) -> None:
        Event.__post_init__(self)  # not super(): zero-arg super breaks under slots=True on 3.12
        if self.qty <= 0:
            raise ValueError(f"order qty must be positive, got {self.qty}")
        # Bracket enforcement (design 4.4): no naked orders reach the OMS.
        if self.stop_loss is None or self.take_profit is None:
            raise ValueError(
                "Order rejected at construction: both stop_loss and take_profit "
                "are mandatory. A plan to close it manually later is not a stop."
            )
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        if self.tif is TimeInForce.GTD and self.ttl is None:
            raise ValueError("GTD order requires ttl")
        _validate_bracket(self.side, self.limit_price, self.stop_loss, self.take_profit)


def _validate_bracket(
    side: Side, ref: float | None, sl: float, tp: float
) -> None:
    """Stop and target must be on the correct sides of the entry."""
    if ref is None:
        return  # market order: entry unknown until fill; OMS re-checks on fill
    if side is Side.BUY:
        if not (sl < ref < tp):
            raise ValueError(f"BUY bracket invalid: need sl<{ref}<tp, got sl={sl} tp={tp}")
    else:
        if not (tp < ref < sl):
            raise ValueError(f"SELL bracket invalid: need tp<{ref}<sl, got sl={sl} tp={tp}")


# --------------------------------------------------------------------------- #
# 4. Fill  -  Broker adapter -> Portfolio, Audit
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True, kw_only=True)
class Fill(Event):
    order_id: str  # client_order_id
    symbol: str
    side: Side
    price: float
    qty: float
    fees: float = 0.0
    liquidity: str | None = None  # "maker" / "taker" if the venue reports it
    is_partial: bool = False
    strategy_id: str = ""

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side is Side.BUY else -self.qty

    @property
    def notional(self) -> float:
        return self.price * self.qty


# --------------------------------------------------------------------------- #
# 5. Rejection  -  OMS or Risk -> Audit, Alerting
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True, kw_only=True)
class Rejection(Event):
    reason_code: str
    origin: str  # "risk" | "oms" | "broker"
    originating_event_id: str
    detail: str = ""
    symbol: str | None = None
    strategy_id: str = ""


# --------------------------------------------------------------------------- #
# 6. Heartbeat  -  Clock -> Watchdog
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True, kw_only=True)
class Heartbeat(Event):
    component_id: str
    state: str = "ok"  # "ok" | "degraded" | "down"
    detail: str = ""


# --------------------------------------------------------------------------- #
# Conditions (not bus events, but flow through the risk manager)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True, kw_only=True)
class DataGap(Event):
    """Emitted by a feed adapter when it detects a sequence/timestamp gap.

    The risk manager treats an open DataGap for a symbol as a trading halt for
    that symbol until it is backfilled and cleared.
    """

    symbol: str
    source: str
    expected_ts: datetime
    observed_ts: datetime
    bars_missing: int | None = None
