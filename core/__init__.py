"""Core abstractions shared by the research and execution planes.

Nothing in here talks to a broker or the network. The same event types, bus,
clock, and protocols are used by both the backtester and the live engine so that
a backtest exercises the identical code path (design principle 3).
"""

from core.events import (
    DataGap,
    Direction,
    Event,
    Fill,
    Heartbeat,
    MarketData,
    Order,
    OrderType,
    Rejection,
    Side,
    Signal,
)

__all__ = [
    "Event",
    "MarketData",
    "Signal",
    "Order",
    "Fill",
    "Rejection",
    "Heartbeat",
    "DataGap",
    "Direction",
    "Side",
    "OrderType",
]
