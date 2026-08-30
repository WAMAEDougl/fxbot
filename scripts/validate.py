"""Run the validation pipeline on a strategy and print the promotion verdict.

    python -m scripts.validate --config configs/ma_crossover.example.yaml \
        --n-groups 6 --k-test 2 --trials 40

This wires the pieces from research/validation and research/promotion:
CPCV paths -> DSR + PBO + per-path expectancy -> promotion checklist.

Trials must be your HONEST count including abandoned ideas. Pass --trials.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import yaml

from data.adapters.synthetic import SyntheticFeed
from research.backtest import BacktestConfig, Backtester, InstrumentSpec
from research.costs import CostModel
from research.metrics import returns_series
from research.promotion import PromotionInputs, evaluate
from research.validation.cpcv import CPCV
from research.validation.purged_kfold import make_t1
from scripts.run_backtest import _load_strategy_class


async def _bars(symbols, interval, start, end):
    feed = SyntheticFeed(seed=0, annual_vol=0.25)
    out = []
    for s in symbols:
        out += await feed.backfill(s, start, end, interval)
    return out


async def _run(args) -> None:
    with open(args.config) as fh:
        spec = yaml.safe_load(fh)
    strat_cls = _load_strategy_class(spec["strategy_class"])
    cfg_mod = spec["strategy_class"].split(":")[0]
    cfg_cls = _load_strategy_class(f"{cfg_mod}:{strat_cls.__name__}Config")
    params = spec["params"]
    symbols = params["symbols"][:1]  # single-symbol validation for the demo
    interval = spec.get("backtest", {}).get("interval", "1d")

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    bars = await _bars(symbols, interval, start, end)
    index = pd.DatetimeIndex(sorted({b.ts for b in bars}))

    t1 = make_t1(index, horizon=5)
    cv = CPCV(n_groups=args.n_groups, k_test=args.k_test, t1=t1)
    print(f"{len(bars)} bars, {cv.n_splits} CPCV splits x {args.trials} trials "
          f"-> {cv.n_splits * args.trials + args.trials} backtests")

    # Evaluate each CPCV split's OUT-OF-SAMPLE segment by backtesting on it.
    per_split_oos: list[pd.Series] = []
    param_grid = _grid(cfg_cls, params, args.trials)

    bt_cfg = BacktestConfig(
        interval=interval,
        costs=CostModel(reject_prob=0.0, partial_prob=0.0),
        instruments={s: InstrumentSpec() for s in symbols},
    )

    by_ts = {}
    for b in bars:
        by_ts.setdefault(b.ts, []).append(b)

    for i, split in enumerate(cv.split(pd.Series(range(len(index)), index=index)), 1):
        print(f"  split {i}/{cv.n_splits} (test groups {split.test_groups})", flush=True)
        test_ts = set(index[split.test_idx])
        test_bars = [b for b in bars if b.ts in test_ts]
        if len(test_bars) < 60:
            per_split_oos.append(pd.Series(dtype=float))
            continue
        best_ret = None
        for gp in param_grid:
            bt = Backtester(lambda gp=gp: [strat_cls(cfg_cls(**gp))], bt_cfg)
            res = await bt.run(test_bars)
            r = returns_series(res.equity_curve)
            if best_ret is None or r.mean() > best_ret.mean():
                best_ret = r
        per_split_oos.append(best_ret if best_ret is not None else pd.Series(dtype=float))

    # Performance matrix for PBO: run the full grid once over everything.
    full = []
    for gp in param_grid:
        bt = Backtester(lambda gp=gp: [strat_cls(cfg_cls(**gp))], bt_cfg)
        res = await bt.run(bars)
        full.append(returns_series(res.equity_curve).reindex(index[1:]).fillna(0.0).to_numpy())
    perf_matrix = np.column_stack(full)

    stitched = pd.concat([s for s in per_split_oos if len(s)]).sort_index()
    regime = _regime_expectancy(stitched)

    inp = PromotionInputs(
        oos_returns=stitched.to_numpy(),
        cpcv_path_returns=[s for s in per_split_oos if len(s)],
        perf_matrix=perf_matrix,
        n_trials=args.trials,
        n_trades=args.assume_trades,
        max_dd_tolerance=args.max_dd,
        regime_metric=regime,
        hypothesis_holds=args.hypothesis_holds,
        periods_per_year=252,
    )
    result = evaluate(inp)
    print("\n=== VALIDATION ===")
    print(result.report_line())
    for k, v in result.detail.items():
        mark = "OK  " if result.checks[k] else "FAIL"
        print(f"  [{mark}] {k}: {v}")
    print(f"\nverdict: {'PROMOTE' if result.passed else 'REJECT'}")


def _grid(cfg_cls, params, trials):
    base = dict(params)
    fasts = [8, 10, 12, 15, 20, 25]
    slows = [30, 40, 50, 60, 80, 100]
    grid = []
    for f in fasts:
        for s in slows:
            if f >= s:
                continue
            g = dict(base)
            g["fast"], g["slow"] = f, s
            grid.append(g)
            if len(grid) >= trials:
                return grid
    return grid


def _regime_expectancy(returns: pd.Series) -> dict[str, float]:
    if returns.empty:
        return {}
    df = returns.to_frame("r")
    df["year"] = df.index.year
    return {str(y): float(g["r"].mean()) for y, g in df.groupby("year")}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--n-groups", type=int, default=6)
    p.add_argument("--k-test", type=int, default=2)
    p.add_argument("--trials", type=int, default=20, help="HONEST trial count incl. abandoned ideas")
    p.add_argument("--max-dd", type=float, default=0.25)
    p.add_argument("--assume-trades", type=int, default=120)
    p.add_argument("--hypothesis-holds", action="store_true")
    p.add_argument("--start", default="2016-01-01")
    p.add_argument("--end", default="2023-12-31")
    asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    main()
