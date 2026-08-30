import numpy as np
import pandas as pd
import pytest

from research.validation.cpcv import CPCV
from research.validation.dsr import (
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
)
from research.validation.pbo import pbo_cscv
from research.validation.purged_kfold import PurgedKFold, make_t1
from research.validation.walkforward import WalkForward


@pytest.fixture
def index():
    return pd.date_range("2018-01-01", periods=1000, freq="D", tz="UTC")


def test_purged_kfold_removes_overlap(index):
    t1 = make_t1(index, horizon=5)
    pk = PurgedKFold(n_splits=5, t1=t1, embargo_pct=0.02)
    for train, test in pk.split(pd.Series(range(len(index)), index=index)):
        assert set(train).isdisjoint(set(test))
        # no train label window should overlap the test span
        t_lo, t_hi = index[test].min(), index[test].max()
        overlap = ((t1.index[train] >= t_lo) & (t1.index[train] <= t_hi)).sum()
        assert overlap == 0


def test_cpcv_split_counts(index):
    t1 = make_t1(index, horizon=3)
    cv = CPCV(n_groups=6, k_test=2, t1=t1)
    assert cv.n_splits == 15
    assert cv.n_paths == 5
    splits = list(cv.split(pd.Series(range(len(index)), index=index)))
    assert len(splits) == 15
    for s in splits:
        assert set(s.train_idx).isdisjoint(set(s.test_idx))


def test_psr_higher_for_longer_sample():
    rng = np.random.default_rng(0)
    short = rng.normal(0.001, 0.01, 100)
    long = rng.normal(0.001, 0.01, 2000)
    assert probabilistic_sharpe_ratio(long) >= probabilistic_sharpe_ratio(short) - 0.05


def test_expected_max_sharpe_grows_with_trials():
    assert expected_max_sharpe(100, 0.01) > expected_max_sharpe(5, 0.01)


def test_dsr_penalises_many_trials():
    rng = np.random.default_rng(1)
    r = rng.normal(0.0008, 0.01, 1500)
    few = deflated_sharpe_ratio(r, n_trials=2, sr_variance=0.001)
    many = deflated_sharpe_ratio(r, n_trials=500, sr_variance=0.001)
    assert many < few


def test_pbo_high_for_pure_noise():
    rng = np.random.default_rng(2)
    perf = rng.normal(0, 0.01, size=(400, 20))  # 20 configs, all noise
    res = pbo_cscv(perf, s=8)
    assert 0.3 <= res.pbo <= 1.0  # noise selection ~ random or worse


def test_pbo_low_for_genuine_signal():
    rng = np.random.default_rng(3)
    base = rng.normal(0, 0.01, size=(400, 20))
    base[:, 0] += 0.004  # config 0 genuinely better in every period
    res = pbo_cscv(base, s=8)
    assert res.pbo < 0.5


def test_walkforward_windows_are_ordered_and_disjoint():
    wf = WalkForward(train_size=200, test_size=50, purge=5)
    prev_test_end = -1
    for w in wf.splits(1000):
        assert w.train.stop <= w.test.start
        assert w.test.start > prev_test_end
        prev_test_end = w.test.start
