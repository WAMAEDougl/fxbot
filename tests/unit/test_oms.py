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


# ------------------------------------------------------- closes retry patiently #
import asyncio  # noqa: E402

from core.events import Rejection  # noqa: E402
from execution.oms import RetryPolicy  # noqa: E402


def _close(coid="close1"):
    return Order(ts=T0, client_order_id=coid, symbol="EWJ", side=Side.SELL, qty=144,
                 order_type=OrderType.MARKET, strategy_id="s", reduce_only=True)


class _FlakyBroker:
    """Fails with a retryable error N times, then accepts (like a leg that is not amendable
    until the session opens)."""
    broker = "flaky"

    def __init__(self, fail_times, message="timeout: the take-profit order is 'accepted', not amendable yet"):
        self.calls, self.fail_times, self.message = 0, fail_times, message

    async def submit(self, order):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(self.message)
        return "B9"


class _RecClock(SimClock):
    def __init__(self, start):
        super().__init__(start)
        self.sleeps = []

    async def sleep(self, s):
        self.sleeps.append(s)


async def test_a_close_that_cannot_happen_yet_is_retried_in_the_background_until_it_can():
    bus, broker, clock = EventBus(), _FlakyBroker(fail_times=4), _RecClock(T0)
    oms = OMS(bus, broker, clock, close_retry=RetryPolicy(max_attempts=60, base_delay=60.0,
                                                          max_delay=900.0))
    await bus.publish(_close())
    # publish returned without waiting for the retries: the bus is not held up
    assert oms.state("close1").status is OrderStatus.PENDING and broker.calls == 0
    await asyncio.gather(*oms._tasks)
    st = oms.state("close1")
    assert st.status is OrderStatus.SUBMITTED and st.broker_order_id == "B9"
    assert broker.calls == 5
    assert all(d >= 30.0 for d in clock.sleeps)  # patient: minutes, not milliseconds


async def test_close_retries_allow_about_ten_hours_by_default():
    oms = OMS(EventBus(), _FlakyBroker(0), SimClock(T0))
    p = oms.close_retry
    assert p.max_attempts >= 40 and p.max_delay >= 600
    assert oms.retry.max_attempts == 5  # entries keep the short policy


async def test_a_close_that_never_succeeds_ends_in_a_visible_rejection_not_silence():
    bus, rejections = EventBus(), []
    bus.subscribe(Rejection, rejections.append)
    oms = OMS(bus, _FlakyBroker(fail_times=99), SimClock(T0),
              close_retry=RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0))
    await bus.publish(_close())
    await asyncio.gather(*oms._tasks)
    assert oms.state("close1").status is OrderStatus.REJECTED
    assert [r.reason_code for r in rejections] == ["RETRIES_EXHAUSTED"]
    assert rejections[0].symbol == "EWJ" and "3 attempts" in rejections[0].detail


async def test_entry_orders_that_run_out_of_retries_are_also_reported():
    bus, rejections = EventBus(), []
    bus.subscribe(Rejection, rejections.append)
    OMS(bus, _FlakyBroker(fail_times=99), SimClock(T0),
        retry=RetryPolicy(max_attempts=2, base_delay=0.0, max_delay=0.0))
    await bus.publish(_order())  # an entry is still awaited inline
    assert [r.reason_code for r in rejections] == ["RETRIES_EXHAUSTED"]


async def test_no_position_is_final_immediately_with_no_retries():
    bus, rejections = EventBus(), []
    bus.subscribe(Rejection, rejections.append)
    broker = _FlakyBroker(fail_times=99, message="no_position: nothing to close for EWJ")
    oms = OMS(bus, broker, SimClock(T0))
    await bus.publish(_close())
    await asyncio.gather(*oms._tasks)
    assert broker.calls == 1 and oms.state("close1").status is OrderStatus.REJECTED
    assert [r.reason_code for r in rejections] == ["NO_POSITION"]
