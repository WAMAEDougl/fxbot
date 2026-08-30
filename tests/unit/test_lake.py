from datetime import UTC, datetime

import pandas as pd
import pytest

from data.lake import ParquetLake


def _frame(start="2021-01-04", n=20):
    idx = pd.bdate_range(start, periods=n, tz="UTC")
    return pd.DataFrame(
        {
            "ts": idx,
            "open": range(100, 100 + n),
            "high": range(101, 101 + n),
            "low": range(99, 99 + n),
            "close": range(100, 100 + n),
            "volume": [1000] * n,
        }
    )


def test_write_then_read_roundtrip(tmp_path):
    lake = ParquetLake(tmp_path)
    lake.write_bars("ABC", "1d", _frame())
    df = lake.read_bars("ABC", "1d")
    assert len(df) == 20
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]


def test_overlapping_write_dedupes_newest_wins(tmp_path):
    lake = ParquetLake(tmp_path)
    lake.write_bars("ABC", "1d", _frame(n=10))
    f2 = _frame(n=10)  # same timestamps, corrected prices
    f2[["open", "high", "low", "close"]] = [[200, 201, 199, 200]] * 10
    lake.write_bars("ABC", "1d", f2)
    df = lake.read_bars("ABC", "1d")
    assert len(df) == 10
    assert (df["close"] == 200).all()


def test_find_gaps(tmp_path):
    lake = ParquetLake(tmp_path)
    f = _frame(n=20)
    f = f.drop(index=[5, 6, 7]).reset_index(drop=True)  # 3-day hole
    lake.write_bars("ABC", "1d", f)
    gaps = lake.find_gaps(
        "ABC", "1d",
        datetime(2021, 1, 4, tzinfo=UTC),
        datetime(2021, 1, 29, tzinfo=UTC),
    )
    assert len(gaps) >= 1


def test_adjustments_applied_at_read_time_only(tmp_path):
    lake = ParquetLake(tmp_path)
    lake.write_bars("ABC", "1d", _frame(n=10))
    raw = lake.read_bars("ABC", "1d")
    lake.write_adjustments(
        "ABC",
        pd.DataFrame(
            {"effective_ts": [pd.Timestamp("2021-01-11", tz="UTC")],
             "price_factor": [0.5], "volume_factor": [2.0]}
        ),
    )
    adj = lake.read_bars("ABC", "1d", adjusted=True)
    raw2 = lake.read_bars("ABC", "1d", adjusted=False)
    pd.testing.assert_frame_equal(raw, raw2)  # stored data untouched
    assert adj["close"].iloc[0] == pytest.approx(raw["close"].iloc[0] * 0.5)


def test_ohlc_validation_rejects_bad_bars(tmp_path):
    lake = ParquetLake(tmp_path)
    bad = _frame(n=5)
    bad.loc[2, "high"] = 0.0
    with pytest.raises(ValueError):
        lake.write_bars("ABC", "1d", bad)
