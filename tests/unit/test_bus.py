from datetime import UTC, datetime

import pytest

from core.bus import EventBus
from core.events import Heartbeat, MarketData


def _bar(sym="X"):
    return MarketData(ts=datetime(2021, 1, 1, tzinfo=UTC), symbol=sym, source="t", close=10.0)


async def test_dispatch_in_subscription_order():
    bus = EventBus()
    seen = []
    bus.subscribe(MarketData, lambda e: seen.append("a"))
    bus.subscribe(MarketData, lambda e: seen.append("b"))
    await bus.publish(_bar())
    assert seen == ["a", "b"]


async def test_cascade_drains_fully_before_return():
    bus = EventBus()
    order = []

    async def on_bar(e):
        order.append("bar")
        await bus.publish(Heartbeat(ts=e.ts, component_id="c"))
        order.append("bar-done")

    def on_hb(e):
        order.append("hb")

    bus.subscribe(MarketData, on_bar)
    bus.subscribe(Heartbeat, on_hb)
    await bus.publish(_bar())
    # heartbeat is queued, handled after on_bar returns, but before publish() returns
    assert order == ["bar", "bar-done", "hb"]


async def test_handler_exception_routed_to_hook_and_bus_survives():
    errors = []
    bus = EventBus(error_hook=lambda *a: errors.append(a))
    bus.subscribe(MarketData, lambda e: 1 / 0)
    bus.subscribe(MarketData, lambda e: errors.append("second-ran"))
    await bus.publish(_bar())
    assert len(errors) == 2 and errors[1] == "second-ran"


async def test_no_error_hook_reraises():
    bus = EventBus()
    bus.subscribe(MarketData, lambda e: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        await bus.publish(_bar())
