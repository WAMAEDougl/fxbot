"""Build the data lake for one or more symbols.

    python -m scripts.backfill --venue synthetic --symbols SYNTH --start 2015-01-01
    python -m scripts.backfill --venue ibkr --symbols AAPL MSFT EUR.USD --start 2015-01-01

The IBKR path needs a running TWS/Gateway and `pip install "trading-system[ibkr]"`.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

import pandas as pd

from data.lake import ParquetLake


def _venue(name: str):
    if name == "synthetic":
        from data.adapters.synthetic import SyntheticFeed

        return SyntheticFeed(seed=0)
    if name == "ibkr":
        from data.adapters.ibkr import IbkrFeed

        return IbkrFeed()
    if name == "csv":
        raise SystemExit("use --csv PATH per symbol for the csv venue")
    raise SystemExit(f"unknown venue: {name}")


async def _run(args) -> None:
    lake = ParquetLake(args.lake)
    feed = _venue(args.venue)
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = (
        datetime.fromisoformat(args.end).replace(tzinfo=UTC)
        if args.end
        else datetime.now(UTC)
    )
    for sym in args.symbols:
        events = await feed.backfill(sym, start, end, args.interval)
        df = pd.DataFrame(
            {
                "ts": [e.ts for e in events],
                "open": [e.open for e in events],
                "high": [e.high for e in events],
                "low": [e.low for e in events],
                "close": [e.close for e in events],
                "volume": [e.volume or 0.0 for e in events],
            }
        )
        n = lake.write_bars(sym, args.interval, df)
        cov = lake.coverage(sym, args.interval)
        print(f"{sym}: wrote {n} bars, coverage {cov}")

        gaps = lake.find_gaps(sym, args.interval, start, end)
        if gaps:
            print(f"  {len(gaps)} gap(s) remain, e.g. {gaps[:3]}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--venue", default="synthetic")
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", default=None)
    p.add_argument("--interval", default="1d")
    p.add_argument("--lake", default="data_lake")
    asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    main()
