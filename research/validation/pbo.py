"""Probability of Backtest Overfitting via Combinatorially Symmetric Cross
Validation (Bailey, Borwein, Lopez de Prado, Zhu 2017).

Input: a performance matrix ``M`` of shape (T observations, N configurations),
e.g. per-bar returns for each parameter set you tried.

Method: split the T rows into ``S`` disjoint contiguous submatrices. For every
way to choose S/2 of them as in-sample (IS), the rest are out-of-sample (OS):
  * pick the config with the best IS performance,
  * find that config's *rank* in the OS set,
  * logit of its relative rank -> negative logit means it underperformed OS.
PBO = fraction of combinations where the IS-best config lands below the OS median.

PBO > 0.5 means your selection process is worse than random.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import numpy as np


@dataclass(frozen=True, slots=True)
class PBOResult:
    pbo: float
    logits: np.ndarray
    n_combinations: int

    @property
    def is_overfit(self) -> bool:
        return self.pbo > 0.5


def _sharpe(x: np.ndarray) -> np.ndarray:
    mu = x.mean(axis=0)
    sd = x.std(axis=0, ddof=1)
    sd[sd == 0] = np.nan
    return mu / sd


def pbo_cscv(
    perf: np.ndarray, s: int = 10, metric=_sharpe
) -> PBOResult:
    perf = np.asarray(perf, dtype=float)
    T, N = perf.shape
    if N < 2:
        raise ValueError("need at least 2 configurations")
    if s % 2 != 0:
        raise ValueError("s must be even")
    if s > T:
        s = T - (T % 2 or 2)

    rows = np.array_split(np.arange(T), s)
    blocks = [perf[r, :] for r in rows]
    logits = []

    for is_sel in combinations(range(s), s // 2):
        os_sel = [i for i in range(s) if i not in is_sel]
        is_mat = np.vstack([blocks[i] for i in is_sel])
        os_mat = np.vstack([blocks[i] for i in os_sel])

        is_perf = metric(is_mat)
        os_perf = metric(os_mat)
        if np.all(np.isnan(is_perf)):
            continue
        best = int(np.nanargmax(is_perf))

        os_rank = (np.argsort(np.argsort(os_perf)) + 1)[best]  # 1..N
        w = os_rank / (N + 1)
        w = min(max(w, 1e-6), 1 - 1e-6)
        logits.append(np.log(w / (1.0 - w)))

    logits = np.array(logits)
    pbo = float(np.mean(logits <= 0.0)) if len(logits) else float("nan")
    return PBOResult(pbo=pbo, logits=logits, n_combinations=len(logits))
