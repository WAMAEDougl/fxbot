"""Synthetic feed: geometric Brownian motion bars. No network, fully deterministic
given a seed. Used to exercise the backtester and validation pipeline end to end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from core.events import MarketData


class SyntheticFeed:
    venue = "synthetic"

    def __init__(
        self,
        *,
        seed: int = 0,
        annual_drift: float = 0.05,
        annual_vol: float = 0.20,
        start_price: float = 100.0,
        spread_bps: float = 2.0,
    ) -> None:
        self.seed = seed
        self.mu = annual_drift
        self.sigma = annual_vol
        self.start_price = start_price
        self.spread_bps = spread_bps

    def _bars_frame(
        self, symbol: str, start: datetime, end: datetime, interval: str
    ) -> pd.DataFrame:
        if interval == "1d":
            idx = pd.bdate_range(start, end, tz="UTC")
            dt = 1 / 252
        elif interval == "1h":
            idx = pd.date_range(start, end, freq="1h", tz="UTC")
            dt = 1 / (252 * 6.5)
        else:
            raise ValueError(f"synthetic feed supports 1d/1h, not {interval}")

        n = len(idx)
        rng = np.random.default_rng(self.seed + hash(symbol) % 2**32)
        shocks = rng.normal(
            (self.mu - 0.5 * self.sigma**2) * dt,
            self.sigma * np.sqrt(dt),
            size=n,
        )
        close = self.start_price * np.exp(np.cumsum(shocks))
        open_ = np.concatenate([[self.start_price], close[:-1]])
        intrabar = np.abs(rng.normal(0, self.sigma * np.sqrt(dt) * 0.7, size=n)) * close
        high = np.maximum(open_, close) + intrabar
        low = np.minimum(open_, close) - intrabar
        low = np.clip(low, 1e-6, None)
        volume = rng.integers(1_000, 100_000, size=n).astype(float)

        return pd.DataFrame(
            {"ts": idx, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
        )

    async def backfill(
        self, symbol: str, start: datetime, end: datetime, interval: str = "1d"
    ) -> list[MarketData]:
        df = self._bars_frame(symbol, start, end, interval)
        return self._to_events(symbol, df, interval)

    def _to_events(self, symbol: str, df: pd.DataFrame, interval: str) -> list[MarketData]:
        half = self.spread_bps / 2 / 10_000
        out: list[MarketData] = []
        for row in df.itertuples(index=False):
            out.append(
                MarketData(
                    ts=row.ts.to_pydatetime(),
                    received_ts=row.ts.to_pydatetime(),
                    symbol=symbol,
                    source=self.venue,
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=float(row.volume),
                    bid=float(row.close) * (1 - half),
                    ask=float(row.close) * (1 + half),
                    bar_interval=interval,
                )
            )
        return out

    async def stream(self, symbols: list[str]) -> AsyncIterator[MarketData]:
        # A finite "live" stream: one year of daily bars ending today.
        end = datetime.now(UTC)
        start = end.replace(year=end.year - 1)
        for sym in symbols:
            for ev in await self.backfill(sym, start, end, "1d"):
                yield ev
