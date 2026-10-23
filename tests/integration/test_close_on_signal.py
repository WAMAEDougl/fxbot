from datetime import UTC, datetime, timedelta

from core.events import Direction, MarketData, Signal
from research.backtest import BacktestConfig, Backtester
from research.costs import CostModel
from risk.manager import RiskConfig

T0 = datetime(2023, 1, 2, tzinfo=UTC)


def _bar(i, o, h, l, c):  # noqa: E741
    return MarketData(ts=T0 + timedelta(days=i), symbol="X", source="t", open=o, high=h,
                      low=l, close=c, volume=1e7, bar_interval="1d")


def _tape(overrides=None, n=40):
    """Flat tape around 100 with +/-1 range; ``overrides`` maps bar index -> (o, h, l, c)."""
    overrides = overrides or {}
    return [_bar(i, *overrides.get(i, (100, 101, 99, 100))) for i in range(n)]


class _EnterThenExit:
    strategy_id = "enter_exit"

    def __init__(self, enter_at, exit_at):
        self.enter_at, self.exit_at, self.i = enter_at, exit_at, -1

    def on_market_data(self, ev, ctx):
        self.i += 1
        if self.i == self.enter_at and ctx.position("X") == 0:
            return [Signal(ts=ev.ts, symbol="X", direction=Direction.LONG,
                           strategy_id="enter_exit", entry_hint=100.0, stop_hint=90.0,
                           target_hint=130.0)]
        if self.i == self.exit_at:
            return [Signal(ts=ev.ts, symbol="X", direction=Direction.FLAT,
                           strategy_id="enter_exit")]
        return []

    def on_fill(self, event):
        return None


def _cfg(latency=1):
    return BacktestConfig(
        starting_equity=100_000, interval="1d",
        risk=RiskConfig(min_stop_spread_mult=0.0, order_ttl=timedelta(days=5)),
        costs=CostModel(reject_prob=0.0, partial_prob=0.0, latency_bars=latency),
    )


async def test_flat_signal_closes_the_position_at_the_next_open_as_a_signal_exit():
    # price steps up to 110 from bar 26, after the entry and before the exit order works
    tape = _tape({i: (110, 111, 109, 110) for i in range(26, 40)})
    res = await Backtester(lambda: [_EnterThenExit(20, 24)], _cfg()).run(tape)
    assert len(res.trades) == 1
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "SIGNAL"
    assert t["exit_ts"] == T0 + timedelta(days=25)  # signal bar 24 -> works at bar 25
    assert res.orders == 2  # one entry (with bracket) and one close
    assert res.stats.n_trades == 1


async def test_close_happens_before_the_same_bars_stop_check():
    # bar 25 gaps through the stop (low 85 < 90), but the close works at the OPEN first
    tape = _tape({25: (100, 101, 85, 90)})
    res = await Backtester(lambda: [_EnterThenExit(20, 24)], _cfg()).run(tape)
    assert list(res.trades["exit_reason"]) == ["SIGNAL"]


async def test_close_arriving_after_a_stop_out_does_not_open_or_flip_a_position():
    # latency 2: the close would work at bar 26, but the stop is hit on bar 25 first
    tape = _tape({25: (100, 101, 85, 88)})
    res = await Backtester(lambda: [_EnterThenExit(20, 24)], _cfg(latency=2)).run(tape)
    assert list(res.trades["exit_reason"]) == ["STOP"]  # exactly one trade, ended by the stop
    codes = list(res.rejections["code"]) if "code" in res.rejections else []
    assert "NO_POSITION" in codes
    assert res.stats.n_trades == 1  # no phantom short opened by the late close


async def test_flat_signal_with_no_position_does_nothing():
    res = await Backtester(lambda: [_EnterThenExit(enter_at=999, exit_at=24)], _cfg()).run(_tape())
    assert len(res.trades) == 0 and res.orders == 0
