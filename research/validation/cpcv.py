"""Combinatorial Purged Cross-Validation (Lopez de Prado, AFML ch. 12).

Split the timeline into ``n_groups`` contiguous groups. Choose every combination
of ``k`` groups as the test set (the rest are train), purging and embargoing at
each train/test boundary. With ``n_groups=6, k=2`` that is C(6,2)=15 splits, and
the held-out groups reassemble into C(6,2)*k/n_groups = 5 distinct backtest
*paths* - giving a distribution of Sharpe ratios instead of one number.

More expensive than walk-forward, but measurably better on PBO and DSR.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import numpy as np
import pandas as pd

from research.validation.purged_kfold import _embargo_times


@dataclass(frozen=True, slots=True)
class CPCVSplit:
    train_idx: np.ndarray
    test_idx: np.ndarray
    test_groups: tuple[int, ...]


class CPCV:
    def __init__(
        self,
        n_groups: int = 6,
        k_test: int = 2,
        t1: pd.Series | None = None,
        embargo_pct: float = 0.01,
    ) -> None:
        if k_test >= n_groups:
            raise ValueError("k_test must be < n_groups")
        if t1 is None:
            raise ValueError("CPCV requires t1")
        self.n_groups = n_groups
        self.k_test = k_test
        self.t1 = t1
        self.embargo_pct = embargo_pct

    @property
    def n_splits(self) -> int:
        from math import comb

        return comb(self.n_groups, self.k_test)

    @property
    def n_paths(self) -> int:
        return self.n_splits * self.k_test // self.n_groups

    def split(self, X: pd.DataFrame | pd.Series):
        idx = pd.DatetimeIndex(self.t1.index)
        n = len(idx)
        if len(X) != n:
            raise ValueError("X and t1 length mismatch")
        groups = np.array_split(np.arange(n), self.n_groups)
        embargo = _embargo_times(idx, self.embargo_pct)

        # int64 nanoseconds throughout - no tz-naive/aware comparison hazards
        obs_ns = idx.asi8
        label_ns = pd.DatetimeIndex(self.t1.values).asi8
        emb_ns = pd.DatetimeIndex(embargo.values).asi8

        for combo in combinations(range(self.n_groups), self.k_test):
            test_pos = np.concatenate([groups[g] for g in combo])
            test_pos.sort()
            train_pos = np.setdiff1d(np.arange(n), test_pos, assume_unique=True)

            drop = np.zeros(n, dtype=bool)
            for g in combo:
                block = groups[g]
                t0 = obs_ns[block[0]]
                t_end = int(label_ns[block].max())
                overlap = (
                    ((label_ns >= t0) & (obs_ns <= t_end))
                    | ((obs_ns >= t0) & (obs_ns <= t_end))
                )
                emb_end = int(emb_ns[block].max())
                emb = (obs_ns > t_end) & (obs_ns <= emb_end)
                drop |= overlap | emb
            train_pos = train_pos[~drop[train_pos]]
            yield CPCVSplit(train_pos, test_pos, combo)


def cpcv_paths(
    per_group_returns: list[pd.Series], n_groups: int, k_test: int
) -> list[pd.Series]:
    """Reassemble held-out test-group return segments into full backtest paths.

    ``per_group_returns[s]`` is the out-of-sample return series for split ``s``
    (concatenated over its ``k_test`` test groups, in time order). Returns
    ``n_paths`` reconstructed equity-return series covering the whole timeline.
    """
    from math import comb

    combos = list(combinations(range(n_groups), k_test))
    n_paths = comb(n_groups, k_test) * k_test // n_groups

    # group g appears in this many splits; distribute its occurrences across paths
    group_slots: dict[int, list[int]] = {g: [] for g in range(n_groups)}
    for s, combo in enumerate(combos):
        for g in combo:
            group_slots[g].append(s)

    paths: list[list[pd.Series]] = [[] for _ in range(n_paths)]
    for g in range(n_groups):
        for p, s in enumerate(group_slots[g]):
            # slice this group's chunk out of split s's OS series
            seg = _group_segment(per_group_returns[s], combos[s], g)
            paths[p].append(seg)
    return [pd.concat(sorted(segs, key=lambda x: x.index[0])) for segs in paths if segs]


def _group_segment(os_series: pd.Series, combo: tuple[int, ...], g: int) -> pd.Series:
    """Given the concatenated OS series for a split and which test groups it
    covered, return just group ``g``'s slice. Assumes equal-ish group sizes."""
    parts = np.array_split(os_series, len(combo))
    pos = combo.index(g)
    return parts[pos]
