"""Build-order step 3 sanity: run the trivial strategy through the full
event-driven backtester and check the plumbing, not the P/L.
"""

from datetime import UTC, datetime

import pytest

from data.adapters.synthetic import SyntheticFeed
from research.backtest import BacktestConfig, Backtester, InstrumentSpec
from research.costs import CostModel
from strategies.ma_crossover import MaCrossover, MaCrossoverConfig

pytestmark = pytest.mark.asyncio


async def _bars(seed=1, symbols=("SYNTH",)):
    feed = SyntheticFeed(seed=seed, annual_vol=0.28, start_price=100.0)
    start = datetime(2018, 1, 1, tzinfo=UTC)
    end = datetime(2023, 12, 31, tzinfo=UTC)
    out = []
    for s in symbols:
        out += await feed.backfill(s, start, end, "1d")
    return out


def _factory(symbols):
    cfg = MaCrossoverConfig(symbols=list(symbols), fast=10, slow=30, allow_short=True)
    return lambda: [MaCrossover(cfg)]


async def test_backtest_runs_and_produces_trades():
    symbols = ("SYNTH",)
    bars = await _bars(symbols=symbols)
    bt = Backtester(
        _factory(symbols),
        BacktestConfig(
            starting_equity=100_000,
            interval="1d",
            costs=CostModel(reject_prob=0.0, partial_prob=0.0),
            instruments={"SYNTH": InstrumentSpec(value_per_point=1.0, lot_step=1.0, min_lot=1.0)},
        ),
    )
    res = await bt.run(bars)
    assert res.signals > 0
    assert res.orders > 0
    assert res.fills > 0
    assert len(res.equity_curve) > 100
    assert res.stats.n_trades >= 1
    # every closed trade must have a finite R multiple
    assert res.trades["r_multiple"].notna().all()


async def test_backtest_is_deterministic():
    symbols = ("SYNTH",)
    bars = await _bars(symbols=symbols)
    cfg = BacktestConfig(
        costs=CostModel(),
        instruments={"SYNTH": InstrumentSpec()},
    )
    r1 = await Backtester(_factory(symbols), cfg).run(bars)
    r2 = await Backtester(_factory(symbols), cfg).run(bars)
    assert r1.equity_curve.equals(r2.equity_curve)
    assert r1.trades.equals(r2.trades)


async def test_costs_reduce_returns():
    symbols = ("SYNTH",)
    bars = await _bars(symbols=symbols)
    cheap = BacktestConfig(
        costs=CostModel(spread_bps=0.1, commission_bps=0.0, impact_coef=0.0,
                        swap_bps_per_day=0.0, reject_prob=0.0, partial_prob=0.0),
        instruments={"SYNTH": InstrumentSpec()},
    )
    dear = BacktestConfig(
        costs=CostModel(spread_bps=8.0, commission_bps=2.0, impact_coef=0.5,
                        swap_bps_per_day=0.5, reject_prob=0.0, partial_prob=0.0),
        instruments={"SYNTH": InstrumentSpec()},
    )
    r_cheap = await Backtester(_factory(symbols), cheap).run(bars)
    r_dear = await Backtester(_factory(symbols), dear).run(bars)
    assert r_dear.stats.total_return < r_cheap.stats.total_return
