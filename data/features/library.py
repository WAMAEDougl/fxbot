"""Built-in causal features. Every rolling window here is backward-looking.

Anything using ``shift(-n)``, full-sample stats, or ``bfill`` is a leak and does
not belong in this file.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from data.features.registry import feature


@feature("log_return")
def log_return(df: pd.DataFrame, n: int = 1) -> pd.Series:
    """Log return over the trailing ``n`` bars."""
    return np.log(df["close"]).diff(n)


@feature("sma")
def sma(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Simple moving average of close over a trailing window."""
    return df["close"].rolling(window, min_periods=window).mean()


@feature("ema")
def ema(df: pd.DataFrame, span: int = 20) -> pd.Series:
    """Exponential moving average of close. ``adjust=False`` -> strictly recursive."""
    return df["close"].ewm(span=span, adjust=False, min_periods=span).mean()


@feature("atr")
def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average true range (Wilder). Used for stop distance and noise checks."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


@feature("rsi")
def rsi(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Wilder's RSI over a trailing window."""
    delta = df["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


@feature("realised_vol")
def realised_vol(df: pd.DataFrame, window: int = 20, annualise: int = 252) -> pd.Series:
    """Trailing realised volatility of log returns, annualised."""
    r = np.log(df["close"]).diff()
    return r.rolling(window, min_periods=window).std(ddof=1) * np.sqrt(annualise)


@feature("zscore")
def zscore(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Trailing z-score of close vs its own rolling mean/std."""
    roll = df["close"].rolling(window, min_periods=window)
    return (df["close"] - roll.mean()) / roll.std(ddof=1)


@feature("donchian_high")
def donchian_high(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Highest high of the *prior* ``window`` bars (excludes the current bar)."""
    return df["high"].shift(1).rolling(window, min_periods=window).max()


@feature("donchian_low")
def donchian_low(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Lowest low of the *prior* ``window`` bars (excludes the current bar)."""
    return df["low"].shift(1).rolling(window, min_periods=window).min()
