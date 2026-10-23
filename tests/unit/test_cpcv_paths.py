from itertools import combinations

import numpy as np
import pandas as pd

from research.validation.cpcv import cpcv_paths


def test_cpcv_paths_returns_full_length_series_per_path():
    n_groups, k = 6, 2
    idx = pd.date_range("2020-01-01", periods=600, freq="B", tz="UTC")
    groups = np.array_split(np.arange(len(idx)), n_groups)
    per_split = []
    for combo in combinations(range(n_groups), k):
        pos = np.concatenate([groups[g] for g in combo])
        per_split.append(pd.Series(np.arange(len(pos), dtype=float), index=idx[pos]))
    paths = cpcv_paths(per_split, n_groups, k)
    assert len(paths) == 5  # C(6,2) * 2 / 6
    for p in paths:
        assert isinstance(p, pd.Series) and p.index.is_monotonic_increasing
        assert abs(len(p) - len(idx)) <= n_groups  # covers the whole timeline
