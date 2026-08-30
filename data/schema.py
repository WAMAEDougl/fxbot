"""Canonical bar schema. Everything downstream assumes exactly this."""

from __future__ import annotations

import pandas as pd

BAR_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]

# pandas dtypes for a validated bar frame
BAR_DTYPES = {
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
}


class BarSchemaError(ValueError):
    pass


def validate_bars(df: pd.DataFrame, *, symbol: str = "") -> pd.DataFrame:
    """Return a normalised copy: UTC ``ts`` index -> column, sorted, deduped,
    OHLC sanity-checked. Raises ``BarSchemaError`` on anything unfixable.
    """
    df = df.copy()

    if df.index.name == "ts" or isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index().rename(columns={df.index.name or "index": "ts"})

    missing = [c for c in BAR_COLUMNS if c not in df.columns]
    if missing:
        raise BarSchemaError(f"{symbol}: missing bar columns {missing}")

    df = df[BAR_COLUMNS]
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    for col, dtype in BAR_DTYPES.items():
        df[col] = df[col].astype(dtype)

    df = df.sort_values("ts").drop_duplicates(subset="ts", keep="last")
    df = df.reset_index(drop=True)

    bad_ohlc = (
        (df["high"] < df["low"])
        | (df["high"] < df["open"])
        | (df["high"] < df["close"])
        | (df["low"] > df["open"])
        | (df["low"] > df["close"])
    )
    if bad_ohlc.any():
        n = int(bad_ohlc.sum())
        raise BarSchemaError(f"{symbol}: {n} bars violate OHLC ordering (high>=all>=low)")

    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise BarSchemaError(f"{symbol}: non-positive prices present")

    return df
