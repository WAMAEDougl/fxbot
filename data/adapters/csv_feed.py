"""Load bars from a local CSV or Parquet file into ``MarketData`` events.

Expected columns (case-insensitive, common aliases handled):
``ts|date|datetime|timestamp, open, high, low, close, volume``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pandas as pd

from core.events import MarketData
from data.schema import validate_bars

_TS_ALIASES = ("ts", "date", "datetime", "timestamp", "time")


class CsvFeed:
    venue = "csv"

    def __init__(self, path: str | Path, *, symbol: str | None = None, source: str = "csv"):
        self.path = Path(path)
        self.symbol = symbol or self.path.stem.upper()
        self.source = source

    def _load(self) -> pd.DataFrame:
        if self.path.suffix.lower() in (".parquet", ".pq"):
            df = pd.read_parquet(self.path)
        else:
            df = pd.read_csv(self.path)
        df.columns = [c.strip().lower() for c in df.columns]
        for alias in _TS_ALIASES:
            if alias in df.columns:
                df = df.rename(columns={alias: "ts"})
                break
        if "volume" not in df.columns:
            df["volume"] = 0.0
        return validate_bars(df, symbol=self.symbol)

    async def backfill(
        self, symbol: str, start: datetime, end: datetime, interval: str = "1d"
    ) -> list[MarketData]:
        df = self._load()
        df = df[(df["ts"] >= pd.Timestamp(start)) & (df["ts"] <= pd.Timestamp(end))]
        return [
            MarketData(
                ts=r.ts.to_pydatetime(),
                received_ts=r.ts.to_pydatetime(),
                symbol=symbol,
                source=self.source,
                open=float(r.open),
                high=float(r.high),
                low=float(r.low),
                close=float(r.close),
                volume=float(r.volume),
                bar_interval=interval,
            )
            for r in df.itertuples(index=False)
        ]

    async def stream(self, symbols: list[str]) -> AsyncIterator[MarketData]:
        raise NotImplementedError("CsvFeed is backfill-only")
