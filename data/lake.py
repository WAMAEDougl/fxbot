"""The Parquet data lake (design 4.1).

Rules encoded here:

* Raw bars append to ``<root>/bars/<interval>/symbol=<S>/year=<Y>/part-*.parquet``.
  Never mutated in place - a re-backfill of an overlapping range appends and the
  reader dedupes on ``ts`` keeping the newest write.
* Corporate actions / contract rolls are NOT baked into stored prices. They live
  in ``<root>/adjustments/<symbol>.parquet`` and are applied at read time when
  ``adjusted=True``. Baking splits into stored data is a classic irreversible
  mistake.
* Gap detection: ``find_gaps`` compares the stored bar timestamps against an
  expected calendar and returns the missing ranges for the adapter to repair.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from data.schema import validate_bars

_INTERVAL_TO_OFFSET = {
    "1d": pd.Timedelta(days=1),
    "1h": pd.Timedelta(hours=1),
    "30m": pd.Timedelta(minutes=30),
    "15m": pd.Timedelta(minutes=15),
    "5m": pd.Timedelta(minutes=5),
    "1m": pd.Timedelta(minutes=1),
}


class ParquetLake:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        (self.root / "bars").mkdir(parents=True, exist_ok=True)
        (self.root / "adjustments").mkdir(parents=True, exist_ok=True)
        self._write_seq = 0

    # ------------------------------------------------------------------ #
    # write
    # ------------------------------------------------------------------ #
    def write_bars(self, symbol: str, interval: str, df: pd.DataFrame) -> int:
        """Append validated bars. Returns row count written.

        Each write stamps ``_ingested_at`` (UTC, nanosecond-unique) so that an
        overlapping re-fetch is resolved newest-wins at read time regardless of
        the order Parquet files come back in.
        """
        df = validate_bars(df, symbol=symbol)
        if df.empty:
            return 0
        stamp = pd.Timestamp.now(tz="UTC") + pd.Timedelta(nanoseconds=self._write_seq)
        self._write_seq += 1
        df = df.assign(year=df["ts"].dt.year, _ingested_at=stamp)
        written = 0
        for year, chunk in df.groupby("year"):
            part_dir = (
                self.root / "bars" / interval / f"symbol={symbol}" / f"year={year}"
            )
            part_dir.mkdir(parents=True, exist_ok=True)
            path = part_dir / f"part-{uuid.uuid4().hex}.parquet"
            chunk.drop(columns="year").to_parquet(path, index=False)
            written += len(chunk)
        return written

    def write_adjustments(self, symbol: str, df: pd.DataFrame) -> None:
        """``df`` columns: effective_ts (UTC), price_factor, volume_factor.

        A price on or after ``effective_ts`` is unaffected; earlier prices are
        multiplied by the cumulative product of later factors at read time.
        """
        df = df.copy()
        df["effective_ts"] = pd.to_datetime(df["effective_ts"], utc=True)
        df["price_factor"] = df["price_factor"].astype("float64")
        df["volume_factor"] = df.get("volume_factor", 1.0)
        df = df.sort_values("effective_ts").reset_index(drop=True)
        df.to_parquet(self.root / "adjustments" / f"{symbol}.parquet", index=False)

    # ------------------------------------------------------------------ #
    # read
    # ------------------------------------------------------------------ #
    def read_bars(
        self,
        symbol: str,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        adjusted: bool = False,
    ) -> pd.DataFrame:
        base = self.root / "bars" / interval / f"symbol={symbol}"
        if not base.exists():
            return validate_bars(
                pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"]),
                symbol=symbol,
            )

        glob = str(base / "**" / "*.parquet")
        con = duckdb.connect()
        try:
            q = f"SELECT * FROM read_parquet('{glob}', union_by_name=true)"
            df = con.execute(q).fetch_df()
        finally:
            con.close()

        # newest write wins for a duplicated ts
        if "_ingested_at" in df.columns:
            df = (
                df.sort_values("_ingested_at")
                .drop_duplicates(subset="ts", keep="last")
                .drop(columns="_ingested_at")
            )

        df = validate_bars(df, symbol=symbol)  # sorts + dedupes newest-wins
        if start is not None:
            df = df[df["ts"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["ts"] <= pd.Timestamp(end)]
        df = df.reset_index(drop=True)

        if adjusted:
            df = self._apply_adjustments(symbol, df)
        return df

    def _apply_adjustments(self, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        path = self.root / "adjustments" / f"{symbol}.parquet"
        if not path.exists() or df.empty:
            return df
        adj = pd.read_parquet(path)
        df = df.copy()
        for col in ("open", "high", "low", "close"):
            factor = pd.Series(1.0, index=df.index)
            for _, row in adj.iterrows():
                factor.loc[df["ts"] < row["effective_ts"]] *= row["price_factor"]
            df[col] = df[col] * factor
        vfactor = pd.Series(1.0, index=df.index)
        for _, row in adj.iterrows():
            vfactor.loc[df["ts"] < row["effective_ts"]] *= row.get("volume_factor", 1.0)
        df["volume"] = df["volume"] * vfactor
        return df

    # ------------------------------------------------------------------ #
    # gap detection
    # ------------------------------------------------------------------ #
    def find_gaps(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        *,
        expected_index: pd.DatetimeIndex | None = None,
    ) -> list[tuple[datetime, datetime]]:
        """Return (gap_start, gap_end) ranges present in the expected calendar
        but absent from the lake. For daily bars pass ``expected_index`` built
        from the venue's trading calendar; without it a naive freq is used.
        """
        df = self.read_bars(symbol, interval, start, end)
        have = set(df["ts"])

        if expected_index is None:
            if interval == "1d":
                expected_index = pd.bdate_range(start, end, tz="UTC")
            else:
                step = _INTERVAL_TO_OFFSET[interval]
                expected_index = pd.date_range(start, end, freq=step, tz="UTC")

        missing = [ts for ts in expected_index if ts not in have]
        if not missing:
            return []

        gaps: list[tuple[datetime, datetime]] = []
        run_start = missing[0]
        prev = missing[0]
        tol = _INTERVAL_TO_OFFSET.get(interval, pd.Timedelta(days=1)) * 1.5
        for ts in missing[1:]:
            if ts - prev > tol:
                gaps.append((run_start.to_pydatetime(), prev.to_pydatetime()))
                run_start = ts
            prev = ts
        gaps.append((run_start.to_pydatetime(), prev.to_pydatetime()))
        return gaps

    # ------------------------------------------------------------------ #
    def symbols(self, interval: str = "1d") -> list[str]:
        base = self.root / "bars" / interval
        if not base.exists():
            return []
        return sorted(
            p.name.split("=", 1)[1] for p in base.glob("symbol=*") if p.is_dir()
        )

    def coverage(self, symbol: str, interval: str = "1d") -> tuple[datetime, datetime] | None:
        df = self.read_bars(symbol, interval)
        if df.empty:
            return None
        return (df["ts"].iloc[0].to_pydatetime(), df["ts"].iloc[-1].to_pydatetime())


def utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)
