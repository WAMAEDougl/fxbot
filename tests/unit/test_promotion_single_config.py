import numpy as np
import pandas as pd

from research.promotion import PromotionInputs, evaluate


def _inputs(n_trials, ncols=1, seed=0):
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0008, 0.005, size=(1500, ncols))  # a strong, clean edge
    idx = pd.bdate_range("2016-01-04", periods=1500)
    paths = [pd.Series(r[:, 0], index=idx)]
    years = {str(y): 0.0008 for y in range(2016, 2022)}
    return PromotionInputs(
        oos_returns=r[:, 0], cpcv_path_returns=paths, perf_matrix=r, n_trials=n_trials,
        n_trades=300, max_dd_tolerance=0.5, regime_metric=years, hypothesis_holds=True)


def test_single_frozen_config_with_one_trial_has_pbo_not_applicable():
    res = evaluate(_inputs(n_trials=1))
    assert res.checks["pbo_lt_0_5"] is True
    assert "n/a" in res.detail["pbo_lt_0_5"]
    assert res.checks["dsr_gt_0_at_95"] is True  # no multiple-testing penalty for one trial
    assert res.passed


def test_single_column_with_more_trials_does_not_get_a_free_pass():
    """If selection happened elsewhere, 'n/a' must not hide it."""
    res = evaluate(_inputs(n_trials=24))
    assert res.checks["pbo_lt_0_5"] is False
    assert not res.passed


def test_multi_config_path_is_unchanged():
    res = evaluate(_inputs(n_trials=4, ncols=4))
    assert "PBO=" in res.detail["pbo_lt_0_5"]
