"""Property tests the design calls out (section 10):

* no order ever exceeds the max risk fraction
* no order is ever emitted without brackets
* replaying the same tape twice produces identical orders
"""

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from core.events import Direction, Signal
from risk.manager import MarketState, RiskConfig, RiskManager
from risk.sizing import RISK_FRACTION_HARD_CAP, SizingParams

TS = datetime(2021, 6, 1, 15, 0, tzinfo=UTC)


class Ctx:
    def __init__(self, equity):
        self._e = equity

    @property
    def equity(self):
        return self._e

    @property
    def now(self):
        return TS

    def position(self, s):
        return 0.0

    def avg_price(self, s):
        return None

    def unrealised_pnl(self, s):
        return 0.0

    def history(self, s, n):
        return []


def _market(price, atr):
    return MarketState(
        last_bar_ts=TS,
        bar_interval=timedelta(days=1),
        spread=price * 0.0002,
        atr=atr,
        price=price,
        value_per_point=1.0,
        sizing=SizingParams(value_per_point=1.0, lot_step=0.0, min_lot=0.0),
        margin_available=1e15,
        margin_rate=0.0,
    )


@settings(max_examples=200, deadline=None)
@given(
    equity=st.floats(min_value=5_000, max_value=5_000_000),
    price=st.floats(min_value=1.0, max_value=5_000.0),
    atr_frac=st.floats(min_value=0.005, max_value=0.15),
    rr=st.floats(min_value=1.5, max_value=5.0),
    risk_fraction=st.floats(min_value=0.001, max_value=0.05),
)
def test_order_never_exceeds_hard_risk_cap(equity, price, atr_frac, rr, risk_fraction):
    atr = price * atr_frac
    rm = RiskManager(RiskConfig(risk_fraction=risk_fraction, max_risk_fraction=risk_fraction,
                                min_stop_spread_mult=0.0))
    stop = price - 2 * atr
    target = price + 2 * atr * rr
    sig = Signal(ts=TS, symbol="X", direction=Direction.LONG, strategy_id="s",
                 entry_hint=price, stop_hint=stop, target_hint=target)
    d = rm.evaluate(sig, Ctx(equity), _market(price, atr))
    if not d.approved:
        return
    risk_amount = d.order.qty * abs(price - d.order.stop_loss) * 1.0
    # exact up to 8-dp price rounding of the stop and lot-step flooring
    assert risk_amount <= equity * RISK_FRACTION_HARD_CAP * (1 + 1e-6) + 1e-4


@settings(max_examples=100, deadline=None)
@given(
    price=st.floats(min_value=1.0, max_value=1000.0),
    atr_frac=st.floats(min_value=0.01, max_value=0.1),
)
def test_every_approved_order_has_brackets(price, atr_frac):
    atr = price * atr_frac
    rm = RiskManager(RiskConfig(min_stop_spread_mult=0.0))
    sig = Signal(ts=TS, symbol="X", direction=Direction.LONG, strategy_id="s",
                 entry_hint=price, stop_hint=price - 3 * atr, target_hint=price + 6 * atr)
    d = rm.evaluate(sig, Ctx(100_000), _market(price, atr))
    if d.approved:
        assert d.order.stop_loss is not None
        assert d.order.take_profit is not None


@settings(max_examples=50, deadline=None)
@given(seed=st.integers(min_value=0, max_value=10_000))
def test_replay_determinism_same_orders(seed):
    rm_a = RiskManager(RiskConfig(min_stop_spread_mult=0.0))
    rm_b = RiskManager(RiskConfig(min_stop_spread_mult=0.0))
    sig = Signal(ts=TS, symbol="X", direction=Direction.LONG, strategy_id="s",
                 entry_hint=100.0, stop_hint=94.0, target_hint=118.0,
                 meta={"seed": seed})
    a = rm_a.evaluate(sig, Ctx(100_000), _market(100.0, 2.0))
    b = rm_b.evaluate(sig, Ctx(100_000), _market(100.0, 2.0))
    assert (a.order is None) == (b.order is None)
    if a.order:
        assert a.order.client_order_id == b.order.client_order_id
        assert a.order.qty == b.order.qty
        assert a.order.stop_loss == b.order.stop_loss
