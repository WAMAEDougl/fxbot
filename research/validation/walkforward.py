"""Walk-forward analysis - the realism check (design 5.1 step 5).

Re-optimise on a rolling (or anchored) in-sample window, trade forward on the
next unseen out-of-sample window, roll, repeat. Closest simulation of how you'd
actually operate.

``WalkForward.splits`` yields (train_slice, test_slice) index ranges. A runner
callback does the optimise-then-evaluate; this class only owns the windowing,
including a purge gap between train and test.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class WFWindow:
    train: slice
    test: slice
    fold: int


class WalkForward:
    def __init__(
        self,
        train_size: int,
        test_size: int,
        *,
        anchored: bool = False,
        purge: int = 0,
        step: int | None = None,
    ) -> None:
        self.train_size = train_size
        self.test_size = test_size
        self.anchored = anchored
        self.purge = purge
        self.step = step or test_size

    def splits(self, n: int):
        fold = 0
        train_start = 0
        train_end = self.train_size
        while train_end + self.purge + self.test_size <= n:
            test_start = train_end + self.purge
            test_end = test_start + self.test_size
            yield WFWindow(
                train=slice(0 if self.anchored else train_start, train_end),
                test=slice(test_start, test_end),
                fold=fold,
            )
            fold += 1
            train_start += self.step
            train_end += self.step

    def run(
        self,
        data: pd.DataFrame,
        optimise_fn,
        evaluate_fn,
    ) -> pd.DataFrame:
        """``optimise_fn(train_df) -> params``; ``evaluate_fn(test_df, params) ->
        dict of metrics``. Returns one row per fold plus the chosen params."""
        rows = []
        for w in self.splits(len(data)):
            train_df = data.iloc[w.train]
            test_df = data.iloc[w.test]
            params = optimise_fn(train_df)
            metrics = evaluate_fn(test_df, params)
            rows.append({"fold": w.fold, "params": params, **metrics,
                         "test_start": test_df.index[0], "test_end": test_df.index[-1]})
        return pd.DataFrame(rows)


def stitch_oos_returns(fold_returns: list[pd.Series]) -> pd.Series:
    """Concatenate per-fold OOS return series into one continuous track."""
    return pd.concat(fold_returns).sort_index()


def wf_efficiency(is_metric: np.ndarray, oos_metric: np.ndarray) -> float:
    """Walk-forward efficiency: mean OOS / mean IS performance. < 0.5 is fragile."""
    is_m = np.nanmean(is_metric)
    return float(np.nanmean(oos_metric) / is_m) if is_m else float("nan")
