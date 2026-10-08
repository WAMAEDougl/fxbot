from datetime import UTC, datetime, timedelta

import pytest

from core.events import Direction, MarketData, Signal
from execution.live import LiveConfig, LiveRunner
from execution.persist import StateJournal
from ops.alerting import Alerter
from ops.killswitch import KillSwitch
from research.backtest import BacktestConfig
from scripts.run_live import check_env_matches_port

NOW = datetime(2024, 3, 20, 12, tzinfo=UTC)
TODAY = NOW.replace(hour=0)


class _Clock:
    def __init__(self):
        self.t = NOW

    def now(self):
        return self.t

    async def sleep(self, s):
        pass


def _bar(ts, close=100.0):
    return MarketData(ts=ts, symbol="AAPL", source="t", open=100, high=101, low=99,
                      close=close, volume=1e6, bar_interval="1d")


def _bars(n_complete, extra_partial=True):
    """Daily bars ending yesterday, plus an unfinished bar dated today."""
    last = TODAY - timedelta(days=1)
    out = [_bar(last - timedelta(days=n_complete - 1 - i), 100 + i * 0.1)
           for i in range(n_complete)]
    if extra_partial:
        out.append(_bar(TODAY))
    return out


class _Feed:
    def __init__(self, bars):
        self.bars = bars

    async def backfill(self, symbol, start, end, interval):
        return [b for b in self.bars if start <= b.ts <= end]


class _Broker:
    broker = "fake"

    def __init__(self, positions=None, equity=100_000.0):
        self.submitted, self._pos, self._eq = [], positions or {}, equity

    async def submit(self, order):
        self.submitted.append(order)
        return str(len(self.submitted))

    async def cancel(self, coid): ...

    async def positions(self):
        return dict(self._pos)

    async def account(self):
        return {"equity": self._eq}

    async def stream_fills(self):
        return
        yield


class _AlwaysLong:
    strategy_id = "stub"

    def on_market_data(self, ev, ctx):
        if len(ctx.history(ev.symbol, 5)) < 5:
            return []
        p = ev.price
        return [Signal(ts=ev.ts, symbol=ev.symbol, direction=Direction.LONG,
                       strategy_id="stub", entry_hint=p, stop_hint=p - 2, target_hint=p + 4)]

    def on_fill(self, event): ...


def _runner(tmp_path, bars, broker=None, ks=None):
    clock = _Clock()
    runner = LiveRunner(
        LiveConfig(env="paper", symbols=["AAPL"], warmup_bars=5),
        broker=broker or _Broker(), feed=_Feed(bars), strategies=[_AlwaysLong()],
        bt_cfg=BacktestConfig(), journal=StateJournal(tmp_path / "j.sqlite"),
        killswitch=ks or KillSwitch(), alerter=Alerter({}), clock=clock,
    )
    return runner, clock


async def test_warmup_trades_nothing_and_partial_bar_is_ignored(tmp_path):
    bars = _bars(10)
    runner, _ = _runner(tmp_path, bars)
    await runner.start()
    assert runner.broker.submitted == []
    assert runner._cursor["AAPL"] == bars[-2].ts  # last *complete* bar, not today's


async def test_new_completed_bar_produces_bracketed_order_once(tmp_path):
    bars = _bars(10, extra_partial=False)
    runner, clock = _runner(tmp_path, bars)
    await runner.start()
    bars.append(_bar(TODAY, 101))
    clock.t = NOW + timedelta(days=1)  # today's bar is now complete
    assert await runner.poll_once() == 1
    assert await runner.poll_once() == 0  # cursor: no re-publish
    assert len(runner.broker.submitted) == 1
    o = runner.broker.submitted[0]
    assert o.stop_loss is not None and o.take_profit is not None


async def test_kill_switch_blocks_new_orders(tmp_path):
    bars = _bars(10, extra_partial=False)
    ks = KillSwitch(tmp_path / "HALT")
    runner, clock = _runner(tmp_path, bars, ks=ks)
    await runner.start()
    (tmp_path / "HALT").touch()
    bars.append(_bar(TODAY, 101))
    clock.t = NOW + timedelta(days=1)
    await runner.poll_once()
    assert runner.broker.submitted == []


async def test_restart_resumes_from_cursor_and_trades_missed_bar_once(tmp_path):
    bars = _bars(10, extra_partial=False)
    r1, _ = _runner(tmp_path, bars)
    await r1.start()
    bars.append(_bar(TODAY, 101))  # completes while the process is down
    r2, clock = _runner(tmp_path, bars)
    clock.t = NOW + timedelta(days=1)
    await r2.start()
    assert len(r2.broker.submitted) == 1


async def test_existing_broker_position_is_adopted(tmp_path):
    runner, _ = _runner(tmp_path, _bars(10), broker=_Broker(positions={"AAPL": 25.0}))
    await runner.start()
    assert runner.portfolio.position("AAPL") == 25.0
    assert runner.ks.engaged is False  # reconciled cleanly


async def test_position_drift_halts(tmp_path):
    runner, _ = _runner(tmp_path, _bars(10))
    await runner.start()
    runner.broker._pos["AAPL"] = 7.0  # broker diverges from local state
    await runner.reconcile_once()
    assert runner.ks.engaged


async def test_daily_loss_trips_kill_switch(tmp_path):
    runner, _ = _runner(tmp_path, _bars(10))
    await runner.start()
    runner.broker._eq = 95_000.0  # -5% vs the 3% limit
    await runner.reconcile_once()
    assert runner.ks.engaged


def test_env_port_guard():
    check_env_matches_port("paper", 4002)
    check_env_matches_port("live", 4001)
    with pytest.raises(SystemExit):
        check_env_matches_port("paper", 4001)
    with pytest.raises(SystemExit):
        check_env_matches_port("live", 7497)
