"""Purged k-fold with embargo (Lopez de Prado, AFML ch. 7).

Standard k-fold leaks in time series because a label at time t depends on data
in [t, t+h]. Purging drops training samples whose label window overlaps the test
set; the embargo drops a further buffer of training samples immediately after
the test set to kill serial correlation.

``t1`` is a Series indexed by observation start time, valued at the time the
observation's label is known (end of its forward window). For simple bar-return
labels over horizon ``h`` bars, ``t1[i] = index[i + h]``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_t1(index: pd.DatetimeIndex, horizon: int) -> pd.Series:
    idx = pd.DatetimeIndex(index)
    end_pos = np.minimum(np.arange(len(idx)) + horizon, len(idx) - 1)
    return pd.Series(idx[end_pos], index=idx)


def _embargo_times(index: pd.DatetimeIndex, pct: float) -> pd.Series:
    step = max(1, int(len(index) * pct))
    shifted = np.minimum(np.arange(len(index)) + step, len(index) - 1)
    return pd.Series(index[shifted], index=index)


class PurgedKFold:
    def __init__(self, n_splits: int = 5, t1: pd.Series | None = None, embargo_pct: float = 0.01):
        if t1 is None:
            raise ValueError("PurgedKFold requires t1 (label end times)")
        if not t1.index.is_monotonic_increasing:
            raise ValueError("t1 must be sorted by its index")
        self.n_splits = n_splits
        self.t1 = t1
        self.embargo_pct = embargo_pct

    def split(self, X: pd.DataFrame | pd.Series):
        idx = pd.DatetimeIndex(self.t1.index)
        if len(X) != len(idx):
            raise ValueError("X and t1 length mismatch")
        embargo = _embargo_times(idx, self.embargo_pct)
        positions = np.arange(len(idx))
        test_starts = np.array_split(positions, self.n_splits)

        # everything compared as int64 nanoseconds to avoid tz-naive/aware clashes
        obs_ns = idx.asi8
        label_ns = pd.DatetimeIndex(self.t1.values).asi8
        emb_ns = pd.DatetimeIndex(embargo.values).asi8

        for test_pos in test_starts:
            t0 = obs_ns[test_pos[0]]
            t_end = int(label_ns[test_pos].max())
            test_idx = test_pos

            # purge: drop train obs whose label window (or start) touches [t0, t_end]
            train_mask = ~(
                ((label_ns >= t0) & (obs_ns <= t_end))
                | ((obs_ns >= t0) & (obs_ns <= t_end))
            )
            # embargo: drop train obs right after the test window
            emb_end = int(emb_ns[test_pos].max())
            train_mask &= ~((obs_ns > t_end) & (obs_ns <= emb_end))
            yield positions[train_mask], test_idx
