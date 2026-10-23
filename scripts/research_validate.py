"""Validate a strategy on REAL data from the lake and print the promotion verdict.

    python -m scripts.research_validate --config configs/etf_trend.yaml

Unlike scripts/validate.py (a synthetic single-symbol demo), this:
* reads the multi-symbol data lake (scripts.backfill --venue alpaca ...),
* evaluates every cell of ``research.grid`` in the config with a FULL-history
  backtest and builds the (T x N) performance matrix,
* runs CPCV on that matrix: for each split, the config with the best TRAIN Sharpe
  is selected and its TEST returns are recorded (selection happens in-sample only),
* measures the real trade count (not assumed), per-symbol concentration, exit mix,
* counts trials honestly from a persistent log (research/trials/<id>.jsonl) plus
  ``--extra-trials`` for abandoned ideas,
* compares against buy-and-hold of the same basket.

The "economic hypothesis still holds" check is a human sign-off: pass
``--hypothesis-holds`` only after reading the results against
research/hypotheses/<name>.md.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from data.lake import ParquetLake
from research.backtest import BacktestConfig, Backtester, InstrumentSpec
from research.costs import CostModel
from research.promotion import PromotionInputs, evaluate
from research.validation.cpcv import CPCV, cpcv_paths
from research.validation.purged_kfold import make_t1
from risk.manager import RiskConfig
from scripts.run_backtest import _bars_from_lake, _config_name, _load_strategy_class

PPY = 252


def sharpe(r: np.ndarray | pd.Series) -> float:
    r = np.asarray(r, dtype=float)
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return float(r.mean() / sd * np.sqrt(PPY)) if sd > 0 else 0.0


def max_dd(r: pd.Series | np.ndarray) -> float:
    eq = np.cumprod(1.0 + np.asarray(r, dtype=float))
    return float(abs((eq / np.maximum.accumulate(eq) - 1.0).min())) if len(eq) else 0.0


def cagr(r: pd.Series | np.ndarray) -> float:
    r = np.asarray(r, dtype=float)
    yrs = len(r) / PPY
    return float(np.prod(1.0 + r) ** (1.0 / yrs) - 1.0) if yrs > 0 else 0.0


def _log_trials(path: Path, grid: list[dict]) -> int:
    """Append unseen parameter sets; return the cumulative unique count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                seen.add(json.dumps(json.loads(line)["params"], sort_keys=True))
    with path.open("a") as fh:
        for g in grid:
            key = json.dumps(g, sort_keys=True)
            if key not in seen:
                seen.add(key)
                fh.write(json.dumps({"ts": datetime.now(UTC).isoformat(), "params": g}) + "\n")
    return len(seen)


def _daily_returns(equity: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    eq = equity.groupby(level=0).last().reindex(index).ffill().bfill()
    return eq.pct_change().fillna(0.0)


async def _run(args) -> None:
    spec = yaml.safe_load(Path(args.config).read_text())
    strat_cls = _load_strategy_class(spec["strategy_class"])
    cfg_cls = _load_strategy_class(
        spec["strategy_class"].split(":")[0] + ":" + _config_name(strat_cls))
    base = spec["params"]
    symbols = base["symbols"]
    bt_spec = spec.get("backtest", {})
    interval = bt_spec.get("interval", "1d")

    lake = ParquetLake(args.lake)
    bars = _bars_from_lake(lake, symbols, interval)
    if not bars:
        raise SystemExit(f"no bars in {args.lake} for {symbols}; run scripts.backfill first")
    lo = datetime.fromisoformat(args.start).replace(tzinfo=UTC) if args.start else None
    hi = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else None
    bars = [b for b in bars if (lo is None or b.ts >= lo) and (hi is None or b.ts <= hi)]
    index = pd.DatetimeIndex(sorted({b.ts for b in bars}))
    print(f"{len(bars)} bars, {len(symbols)} symbols, {index[0].date()} -> {index[-1].date()}")

    grid_spec = spec.get("research", {}).get("grid", {})
    keys = list(grid_spec)
    grid = [{**base, **dict(zip(keys, vals, strict=True))}
            for vals in itertools.product(*(grid_spec[k] for k in keys))] or [dict(base)]
    cells = [{k: g[k] for k in keys} for g in grid]
    sid = base.get("strategy_id", spec.get("strategy_id", "strategy"))
    n_unique = _log_trials(Path("research/trials") / f"{sid}.jsonl", cells)
    n_trials = n_unique + args.extra_trials
    print(f"trials: {n_unique} unique parameter sets ever evaluated + "
          f"{args.extra_trials} declared abandoned = {n_trials}")

    bt_cfg = BacktestConfig(
        starting_equity=bt_spec.get("starting_equity", 100_000),
        interval=interval,
        risk=RiskConfig(**bt_spec.get("risk", {})),
        costs=CostModel(**bt_spec.get("costs", {})),
        instruments={s: InstrumentSpec(**v) for s, v in bt_spec.get("instruments", {}).items()},
    )

    results, cols = [], []
    for i, (g, cell) in enumerate(zip(grid, cells, strict=True), 1):
        res = await Backtester(lambda g=g: [strat_cls(cfg_cls(**g))], bt_cfg).run(bars)
        results.append(res)
        cols.append(_daily_returns(res.equity_curve, index).to_numpy())
        s = res.stats
        print(f"  [{i}/{len(grid)}] {cell}  trades={s.n_trades} CAGR={s.cagr:+.1%} "
              f"Sharpe={s.sharpe:.2f} maxDD={s.max_drawdown:.1%} exposure={s.exposure:.0%}",
              flush=True)
    perf = np.column_stack(cols)  # (T, N)

    # ---- CPCV: select on train, record test ------------------------------ #
    t1 = make_t1(index, horizon=args.purge)
    cv = CPCV(n_groups=args.n_groups, k_test=args.k_test, t1=t1)
    per_split: list[pd.Series] = []
    picks: Counter[int] = Counter()
    for split in cv.split(pd.Series(range(len(index)), index=index)):
        train_sr = [sharpe(perf[split.train_idx, j]) for j in range(perf.shape[1])]
        best = int(np.argmax(train_sr))
        picks[best] += 1
        per_split.append(pd.Series(perf[split.test_idx, best], index=index[split.test_idx]))
    paths = cpcv_paths(per_split, args.n_groups, args.k_test)
    path_sr = [sharpe(p) for p in paths]
    med = int(np.argsort(path_sr)[len(path_sr) // 2])
    stitched = paths[med]
    chosen = picks.most_common(1)[0][0]
    res_c = results[chosen]
    print(f"\nCPCV: {cv.n_splits} splits -> {len(paths)} paths; most-selected config: "
          f"{cells[chosen]} ({picks[chosen]}/{cv.n_splits} splits)")
    print("path Sharpes:", [round(x, 2) for x in path_sr])

    regime = {str(y): float(g.mean()) for y, g in stitched.groupby(stitched.index.year)}
    result = evaluate(PromotionInputs(
        oos_returns=stitched.to_numpy(), cpcv_path_returns=paths, perf_matrix=perf,
        n_trials=n_trials, n_trades=res_c.stats.n_trades, max_dd_tolerance=args.max_dd,
        regime_metric=regime, hypothesis_holds=args.hypothesis_holds, periods_per_year=PPY,
    ))

    # ---- benchmarks, concentration, exit mix ------------------------------ #
    closes = pd.DataFrame({
        s: lake.read_bars(s, interval, adjusted=True).set_index("ts")["close"] for s in symbols
    }).reindex(index).ffill()
    rets = closes.pct_change().fillna(0.0)
    bench = {"equal-weight basket (B&H)": rets.mean(axis=1)}
    if "SPY" in rets:
        bench["SPY (B&H)"] = rets["SPY"]
    print("\n=== BENCHMARKS (same period, no costs) ===")
    for k, r in bench.items():
        print(f"  {k:<28} CAGR={cagr(r):+.1%} Sharpe={sharpe(r):.2f} maxDD={max_dd(r):.1%}")
    print(f"  {'strategy (chosen, full run)':<28} CAGR={res_c.stats.cagr:+.1%} "
          f"Sharpe={res_c.stats.sharpe:.2f} maxDD={res_c.stats.max_drawdown:.1%}")

    tr = res_c.trades
    if len(tr):
        print("\n=== CHOSEN CONFIG: TRADES ===")
        print(f"  {len(tr)} trades, win rate {res_c.stats.win_rate:.0%}, "
              f"expectancy {res_c.stats.expectancy_r:+.2f}R, avg win {res_c.stats.avg_win:.0f} / "
              f"avg loss {res_c.stats.avg_loss:.0f}")
        print("  exits:", dict(tr["exit_reason"].value_counts()))
        by_sym = tr.groupby("symbol")["pnl"].sum().sort_values(ascending=False)
        print("  P&L by symbol:", {k: round(v) for k, v in by_sym.items()})
        by_dir = tr.groupby("direction")["pnl"].agg(["count", "sum"])
        print("  by direction:", {d: (int(r["count"]), round(r["sum"])) for d, r in by_dir.iterrows()})

    print("\n=== VALIDATION (design 5.3) ===")
    print(result.report_line())
    for k, v in result.detail.items():
        print(f"  [{'OK  ' if result.checks[k] else 'FAIL'}] {k}: {v}")
    objective = [k for k in result.checks if k != "hypothesis_holds"]
    ok = sum(result.checks[k] for k in objective)
    print(f"\nobjective checks passed: {ok}/{len(objective)}  "
          f"(hypothesis_holds is a manual sign-off: {'given' if args.hypothesis_holds else 'NOT given'})")
    print(f"verdict: {'PROMOTE' if result.passed else 'REJECT'}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--lake", default="data_lake")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--n-groups", type=int, default=6)
    p.add_argument("--k-test", type=int, default=2)
    p.add_argument("--purge", type=int, default=21, help="purge/embargo horizon in bars")
    p.add_argument("--max-dd", type=float, default=0.25)
    p.add_argument("--extra-trials", type=int, default=0,
                   help="abandoned ideas not in the log; be honest")
    p.add_argument("--hypothesis-holds", action="store_true")
    asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    main()
