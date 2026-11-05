from datetime import UTC, datetime

from core.clock import Clock
from core.events import Fill, Side
from execution.portfolio import Portfolio
from execution.reconcile import Reconciler
from ops.killswitch import KillSwitch
from ops.status import engine_health

T = datetime(2026, 11, 4, tzinfo=UTC)


def test_feed_trip_clears_when_feed_recovers():
    ks = KillSwitch(feed_stale_after_s=10)
    ks.on_feed_tick("f", now=0)
    ks.check_feed(now=100)
    assert ks.engaged
    ks.on_feed_tick("f", now=101)
    assert not ks.engaged


def test_daily_loss_clears_next_day_but_position_drift_latches():
    ks = KillSwitch()
    ks.on_daily_loss(0.05)
    assert ks.engaged
    ks.new_day()
    assert not ks.engaged
    ks.on_reconcile_divergence("[('X', 1, 0)]")
    ks.new_day()
    for _ in range(5):
        ks.on_reconcile_ok()
    assert ks.engaged  # latched until a person restarts


def test_cash_drift_clears_after_clean_reconciles():
    ks = KillSwitch()
    ks.on_reconcile_divergence("cash", recoverable=True)
    assert ks.engaged and "cash" in ks.state().reason
    ks.on_reconcile_ok()
    ks.on_reconcile_ok()
    assert ks.engaged
    ks.on_reconcile_ok()
    assert not ks.engaged


class _Clock(Clock):
    def now(self):
        return T

    async def sleep(self, s):
        pass


class _Broker:
    def __init__(self, qty, cash, eq):
        self.q, self.cash, self.eq = qty, cash, eq

    async def positions(self):
        return dict(self.q)

    async def account(self):
        return {"equity": self.eq, "cash": self.cash}


async def test_price_move_is_not_drift_but_missing_cash_is():
    p = Portfolio(100_000.0)
    p.seed_position("DIA", 100, 500.0)
    b = _Broker({"DIA": 100}, cash=50_000.0, eq=100_000.0)
    r = Reconciler(b, p, _Clock())
    assert (await r.run_once()).ok
    b.eq = 103_000.0  # the broker marks DIA up 30 points; local marks are a day old
    assert (await r.run_once()).ok
    # a buy fills locally and at the broker: still consistent
    p.on_fill(Fill(ts=T, order_id="o", symbol="XLE", side=Side.BUY, qty=10, price=90.0,
                   fees=0.0, strategy_id="s"))
    b.q["XLE"], b.cash = 10, 50_000.0 - 900.0
    assert (await r.run_once()).ok
    b.cash -= 2_000.0  # money vanished with no fill: needs two checks to count
    assert (await r.run_once()).ok
    res = await r.run_once()
    assert not res.ok and abs(res.cash_delta + 2_000.0) < 1e-6


def test_status_shows_kill_reason():
    h = engine_health(100.0, 100.0, True, kill_reason="reconciliation drift")
    assert not h.ok and "reconciliation drift" in h.reasons[0]
