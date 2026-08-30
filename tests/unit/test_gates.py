from datetime import UTC, datetime, timedelta

from core.events import Direction, Signal
from risk.limits import LimitBook
from risk.manager import MarketState, RiskConfig, RiskManager
from risk.sizing import SizingParams


class FakeCtx:
    def __init__(self, equity=100_000, positions=None, now=None):
        self._equity = equity
        self._pos = positions or {}
        self._now = now or datetime(2021, 6, 1, 15, 0, tzinfo=UTC)

    @property
    def equity(self):
        return self._equity

    @property
    def now(self):
        return self._now

    def position(self, s):
        return self._pos.get(s, 0.0)

    def avg_price(self, s):
        return 100.0 if s in self._pos else None

    def unrealised_pnl(self, s):
        return 0.0

    def history(self, s, n):
        return []


def _market(**kw):
    base = dict(
        last_bar_ts=datetime(2021, 6, 1, 15, 0, tzinfo=UTC),
        bar_interval=timedelta(days=1),
        spread=0.02,
        atr=2.0,
        price=100.0,
        value_per_point=1.0,
        sizing=SizingParams(value_per_point=1.0, lot_step=1.0, min_lot=1.0),
        margin_available=1e9,
        margin_rate=1.0,
    )
    base.update(kw)
    return MarketState(**base)


def _sig(direction=Direction.LONG, **kw):
    base = dict(
        ts=datetime(2021, 6, 1, 15, 0, tzinfo=UTC),
        symbol="X",
        direction=direction,
        strategy_id="s",
    )
    base.update(kw)
    return Signal(**base)


def test_happy_path_produces_bracketed_order():
    rm = RiskManager(RiskConfig())
    d = rm.evaluate(_sig(stop_hint=96.0, target_hint=110.0), FakeCtx(), _market())
    assert d.approved
    assert d.order.stop_loss is not None and d.order.take_profit is not None
    assert d.order.qty > 0


def test_kill_switch_blocks():
    rm = RiskManager(RiskConfig(), kill_switch=lambda: True)
    d = rm.evaluate(_sig(stop_hint=96.0, target_hint=110.0), FakeCtx(), _market())
    assert not d.approved
    assert d.rejection.reason_code == "KILL_SWITCH"


def test_stale_data_blocks():
    rm = RiskManager(RiskConfig())
    stale = _market(last_bar_ts=datetime(2021, 5, 1, tzinfo=UTC))
    d = rm.evaluate(_sig(stop_hint=96.0, target_hint=110.0), FakeCtx(), stale)
    assert d.rejection.reason_code == "STALE_DATA"


def test_tight_stop_blocked_by_atr():
    rm = RiskManager(RiskConfig())
    d = rm.evaluate(_sig(stop_hint=99.5, target_hint=110.0), FakeCtx(), _market(atr=2.0))
    assert d.rejection.reason_code == "NOISE_TIGHT_STOP"


def test_insufficient_rr_blocked():
    rm = RiskManager(RiskConfig(min_rr=1.5))
    # risk 4, reward 4 -> RR 1.0
    d = rm.evaluate(_sig(stop_hint=96.0, target_hint=104.0), FakeCtx(), _market())
    assert d.rejection.reason_code == "INSUFFICIENT_RR"


def test_trade_cap_blocks_after_n():
    lb = LimitBook()
    rm = RiskManager(RiskConfig(max_trades_per_day=2), limits=lb)
    ctx = FakeCtx()
    lb.roll_day(ctx.now, ctx.equity)
    lb.daily.trades_opened = 2
    d = rm.evaluate(_sig(stop_hint=96.0, target_hint=110.0), ctx, _market())
    assert d.rejection.reason_code == "TRADE_CAP"


def test_daily_loss_limit_blocks():
    lb = LimitBook()
    rm = RiskManager(RiskConfig(daily_loss_limit=0.03), limits=lb)
    ctx = FakeCtx(equity=96_000)
    lb.roll_day(ctx.now, 100_000)  # started the day at 100k, now down 4%
    d = rm.evaluate(_sig(stop_hint=92.0, target_hint=110.0), ctx, _market())
    assert d.rejection.reason_code == "DAILY_LOSS_LIMIT"


def test_reversal_blocked_when_holding_opposite():
    rm = RiskManager(RiskConfig())
    ctx = FakeCtx(positions={"X": 100.0})  # long
    d = rm.evaluate(_sig(Direction.SHORT, stop_hint=104.0, target_hint=90.0), ctx, _market())
    assert d.rejection.reason_code == "REVERSAL_BLOCK"


def test_event_window_clips_size():
    rm = RiskManager(RiskConfig())
    normal = rm.evaluate(_sig(stop_hint=96.0, target_hint=110.0), FakeCtx(), _market())
    evt = rm.evaluate(_sig(stop_hint=96.0, target_hint=110.0), FakeCtx(),
                      _market(high_impact_event=True))
    assert evt.approved
    assert evt.order.qty < normal.order.qty


def test_client_order_id_is_deterministic():
    rm = RiskManager(RiskConfig())
    s = _sig(stop_hint=96.0, target_hint=110.0)
    a = rm.evaluate(s, FakeCtx(), _market())
    b = rm.evaluate(s, FakeCtx(), _market())
    assert a.order.client_order_id == b.order.client_order_id
