from datetime import UTC, datetime, timedelta

from core.bus import EventBus
from core.clock import SimClock
from core.events import Fill, Order, OrderType, Side
from execution.oms import OMS, OrderStatus

T0 = datetime(2024, 1, 2, tzinfo=UTC)


class _Broker:
    broker = "fake"

    def __init__(self):
        self.submitted = []

    async def submit(self, order):
        self.submitted.append(order)
        return "B1"


def _order(coid="c1"):
    return Order(ts=T0, client_order_id=coid, symbol="AAPL", side=Side.BUY, qty=10,
                 order_type=OrderType.MARKET, stop_loss=95.0, take_profit=110.0,
                 ttl=T0 + timedelta(days=1), strategy_id="s")


def _fill(side, qty, price, coid="c1"):
    return Fill(ts=T0, order_id=coid, symbol="AAPL", side=side, price=price, qty=qty)


async def test_bracket_exit_fill_does_not_count_toward_entry():
    bus, fills = EventBus(), []
    bus.subscribe(Fill, fills.append)
    oms = OMS(bus, _Broker(), SimClock(T0))
    await bus.publish(_order())
    await oms.on_broker_fill(_fill(Side.BUY, 4, 100))
    assert oms.state("c1").status is OrderStatus.PARTIAL
    await oms.on_broker_fill(_fill(Side.SELL, 4, 110))  # take-profit leg
    st = oms.state("c1")
    assert st.status is OrderStatus.PARTIAL and st.filled_qty == 4
    assert len(fills) == 2  # exit still reaches the portfolio


async def test_fill_inherits_strategy_id_and_entry_completes():
    bus, fills = EventBus(), []
    bus.subscribe(Fill, fills.append)
    oms = OMS(bus, _Broker(), SimClock(T0))
    await bus.publish(_order())
    await oms.on_broker_fill(_fill(Side.BUY, 10, 100))
    assert oms.state("c1").status is OrderStatus.FILLED
    assert fills[0].strategy_id == "s"


async def test_duplicate_order_not_resubmitted():
    bus, broker = EventBus(), _Broker()
    OMS(bus, broker, SimClock(T0))
    await bus.publish(_order())
    await bus.publish(_order())
    assert len(broker.submitted) == 1
