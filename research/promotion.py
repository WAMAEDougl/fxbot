"""Promotion criteria gate (design 5.3).

A strategy moves research -> registry only if ALL hold. Falling short on any one
is a rejection, not a negotiation. This module just evaluates the checklist and
produces a pass/fail report with the failing lines named.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from research.validation.dsr import deflated_sharpe_ratio
from research.validation.pbo import pbo_cscv


@dataclass(slots=True)
class PromotionInputs:
    oos_returns: np.ndarray  # stitched out-of-sample per-period returns
    cpcv_path_returns: list[pd.Series]  # one series per reconstructed CPCV path
    perf_matrix: np.ndarray  # (T, N_configs) for PBO
    n_trials: int  # honest count, incl. abandoned ideas
    n_trades: int
    max_dd_tolerance: float  # e.g. 0.20
    regime_metric: dict[str, float]  # e.g. {"2021": 0.4, "2022": 0.5, ...} expectancy by bucket
    hypothesis_holds: bool
    periods_per_year: int = 252


@dataclass(slots=True)
class PromotionResult:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    detail: dict[str, str] = field(default_factory=dict)
    deflated_sharpe: float = 0.0
    pbo: float = 1.0

    def report_line(self) -> str:
        marks = " ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in self.checks.items())
        return f"{'PROMOTE' if self.passed else 'REJECT'}  DSR={self.deflated_sharpe:.3f} PBO={self.pbo:.3f}  [{marks}]"


def evaluate(inp: PromotionInputs) -> PromotionResult:
    checks: dict[str, bool] = {}
    detail: dict[str, str] = {}

    sr_var = _sr_variance_from_matrix(inp.perf_matrix)
    dsr = deflated_sharpe_ratio(inp.oos_returns, inp.n_trials, sr_variance=sr_var)
    checks["dsr_gt_0_at_95"] = dsr > 0.95
    detail["dsr_gt_0_at_95"] = f"DSR={dsr:.3f} (need > 0.95)"

    pbo_res = pbo_cscv(inp.perf_matrix, s=min(10, _even(inp.perf_matrix.shape[0])))
    checks["pbo_lt_0_5"] = pbo_res.pbo < 0.5
    detail["pbo_lt_0_5"] = f"PBO={pbo_res.pbo:.3f} (need < 0.5)"

    path_exp = [s.mean() for s in inp.cpcv_path_returns if len(s)]
    checks["every_cpcv_path_positive"] = len(path_exp) > 0 and all(e > 0 for e in path_exp)
    detail["every_cpcv_path_positive"] = f"min path expectancy={min(path_exp) if path_exp else float('nan'):.2e}"

    worst_dd = _worst_path_max_dd(inp.cpcv_path_returns)
    checks["worst_path_dd_ok"] = worst_dd <= inp.max_dd_tolerance
    detail["worst_path_dd_ok"] = f"worst-path maxDD={worst_dd:.2%} (tol {inp.max_dd_tolerance:.2%})"

    reg = list(inp.regime_metric.values())
    concentrated = (max(reg) > 3 * np.median(reg)) if len(reg) >= 3 else True
    checks["not_regime_concentrated"] = not concentrated
    detail["not_regime_concentrated"] = f"regime expectancies={ {k: round(v,4) for k,v in inp.regime_metric.items()} }"

    checks["min_100_trades"] = inp.n_trades >= 100
    detail["min_100_trades"] = f"{inp.n_trades} trades (need >= 100)"

    checks["hypothesis_holds"] = inp.hypothesis_holds
    detail["hypothesis_holds"] = "economic rationale confirmed after inspection" if inp.hypothesis_holds else "hypothesis no longer holds"

    passed = all(checks.values())
    return PromotionResult(
        passed=passed, checks=checks, detail=detail,
        deflated_sharpe=dsr, pbo=pbo_res.pbo,
    )


def _sr_variance_from_matrix(perf: np.ndarray) -> float:
    mu = perf.mean(axis=0)
    sd = perf.std(axis=0, ddof=1)
    sd[sd == 0] = np.nan
    sr = mu / sd
    return float(np.nanvar(sr, ddof=1)) if np.isfinite(np.nanvar(sr)) else 1e-4


def _worst_path_max_dd(paths: list[pd.Series]) -> float:
    worst = 0.0
    for r in paths:
        if not len(r):
            continue
        eq = (1.0 + r).cumprod()
        dd = (eq / eq.cummax() - 1.0).min()
        worst = min(worst, dd)
    return abs(worst)


def _even(n: int) -> int:
    return n if n % 2 == 0 else n - 1
