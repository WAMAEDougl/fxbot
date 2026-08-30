"""Run a strategy config through the backtester and print a tearsheet.

    python -m scripts.run_backtest --config configs/ma_crossover.example.yaml \
        --lake data_lake

Falls back to a synthetic feed if the lake has no data for the symbols.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
from datetime import UTC, datetime

import yaml

from core.events import MarketData
from data.lake import ParquetLake
from research.backtest import BacktestConfig, Backtester, InstrumentSpec
from research.costs import CostModel
from risk.manager import RiskConfig


def _load_strategy_class(path: str):
    mod, _, cls = path.partition(":")
    return getattr(importlib.import_module(mod), cls)


def _bars_from_lake(lake: ParquetLake, symbols, interval) -> list[MarketData]:
    out: list[MarketData] = []
    for sym in symbols:
        df = lake.read_bars(sym, interval, adjusted=True)
        for r in df.itertuples(index=False):
            out.append(
                MarketData(
                    ts=r.ts.to_pydatetime(), received_ts=r.ts.to_pydatetime(),
                    symbol=sym, source="lake", open=r.open, high=r.high, low=r.low,
                    close=r.close, volume=r.volume, bar_interval=interval,
                )
            )
    return out


async def _synthetic_bars(symbols, interval) -> list[MarketData]:
    from data.adapters.synthetic import SyntheticFeed

    feed = SyntheticFeed(seed=0, annual_vol=0.25)
    start = datetime(2015, 1, 1, tzinfo=UTC)
    end = datetime.now(UTC)
    out = []
    for s in symbols:
        out += await feed.backfill(s, start, end, interval)
    return out


async def _run(args) -> None:
    with open(args.config) as fh:
        spec = yaml.safe_load(fh)
    strat_cls = _load_strategy_class(spec["strategy_class"])
    params = spec["params"]
    symbols = params["symbols"]
    bt_spec = spec.get("backtest", {})
    interval = bt_spec.get("interval", "1d")

    lake = ParquetLake(args.lake)
    bars = _bars_from_lake(lake, symbols, interval)
    if not bars:
        print("lake empty for these symbols - using synthetic feed")
        bars = await _synthetic_bars(symbols, interval)

    cfg_cls = _load_strategy_class(spec["strategy_class"].split(":")[0] + ":" + _config_name(strat_cls))
    strat_config = cfg_cls(**params)

    instruments = {
        s: InstrumentSpec(**v) for s, v in bt_spec.get("instruments", {}).items()
    }
    bt_cfg = BacktestConfig(
        starting_equity=bt_spec.get("starting_equity", 100_000),
        interval=interval,
        risk=RiskConfig(**bt_spec.get("risk", {})),
        costs=CostModel(**bt_spec.get("costs", {})),
        instruments=instruments,
    )

    bt = Backtester(lambda: [strat_cls(strat_config)], bt_cfg)
    res = await bt.run(bars)

    print("\n=== BACKTEST ===")
    print(res.summary())
    print("\nstats:")
    for k, v in res.stats.as_dict().items():
        print(f"  {k:>16}: {v:.4f}" if isinstance(v, float) else f"  {k:>16}: {v}")
    if not res.rejections.empty:
        print("\nrejections by code:")
        print(res.rejections["code"].value_counts().to_string())
    if args.out:
        res.equity_curve.to_csv(args.out)
        print(f"\nequity curve -> {args.out}")


def _config_name(strat_cls) -> str:
    # convention: MaCrossover -> MaCrossoverConfig
    return strat_cls.__name__ + "Config"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--lake", default="data_lake")
    p.add_argument("--out", default=None)
    asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    main()
