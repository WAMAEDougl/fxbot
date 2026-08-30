import numpy as np
import pandas as pd
import pytest

from data.features.registry import FEATURES, assert_causal, compute, features_hash


@pytest.fixture
def bars():
    n = 300
    rng = np.random.default_rng(0)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame(
        {
            "ts": pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC"),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": rng.integers(1, 1000, n).astype(float),
        }
    )


@pytest.mark.parametrize("name", sorted(FEATURES))
def test_all_features_are_causal(name, bars):
    params = {}
    assert_causal(name, bars, **params)


def test_sma_matches_pandas(bars):
    got = compute("sma", bars, window=20)
    exp = bars["close"].rolling(20, min_periods=20).mean()
    pd.testing.assert_series_equal(got, exp, check_names=False)


def test_feature_version_changes_hash():
    h1 = features_hash(["sma", "atr"], {"sma": 20})
    h2 = features_hash(["sma", "atr"], {"sma": 50})
    assert h1 != h2


def test_donchian_excludes_current_bar(bars):
    dh = compute("donchian_high", bars, window=10)
    # value at row i must not depend on high[i]
    b2 = bars.copy()
    b2.loc[b2.index[50], "high"] = 1e6
    dh2 = compute("donchian_high", b2, window=10)
    assert dh.iloc[50] == dh2.iloc[50]
