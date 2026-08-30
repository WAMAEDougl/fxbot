"""Clocks. Sim and live share one interface (design section 10, core/clock.py).

The strategy engine, risk manager, and OMS never call ``datetime.now()``
directly - they ask the clock. In a backtest the clock is a ``SimClock`` driven
by the event stream; in production it is a ``LiveClock``. Nothing else changes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class LiveClock:
    """Wall-clock UTC. ``sleep`` really sleeps."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SimClock:
    """Deterministic clock for backtests.

    Time only moves when the backtest driver calls ``advance_to``. ``sleep``
    advances virtual time instantly, so timeout logic in the OMS/risk layer
    runs unchanged but a backtest still completes in milliseconds.
    """

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock start must be tz-aware UTC")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, ts: datetime) -> None:
        ts = ts.astimezone(UTC)
        if ts < self._now:
            raise ValueError(f"clock cannot go backwards: {ts} < {self._now}")
        self._now = ts

    async def sleep(self, seconds: float) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)
