from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from data.adapters.synthetic import SyntheticFeed


@pytest.fixture
def utc_start() -> datetime:
    return datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def utc_end() -> datetime:
    return datetime(2023, 12, 31, tzinfo=UTC)


@pytest.fixture
def synth_bars(utc_start, utc_end):
    feed = SyntheticFeed(seed=42, annual_vol=0.25, start_price=100.0)
    return asyncio.get_event_loop().run_until_complete(
        feed.backfill("TEST", utc_start, utc_end, "1d")
    )


@pytest.fixture
def multi_symbol_bars(utc_start, utc_end):
    feed = SyntheticFeed(seed=7, annual_vol=0.2)
    loop = asyncio.get_event_loop()
    out = []
    for sym in ("AAA", "BBB", "CCC"):
        out += loop.run_until_complete(feed.backfill(sym, utc_start, utc_end, "1d"))
    return out
