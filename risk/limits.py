"""Stateful risk bookkeeping the gates read: daily loss, trade counts, cooldowns,
correlation clusters. Kept separate from the gate logic so it can be persisted
and replayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


@dataclass
class DailyState:
    day: date
    start_equity: float
    realised_pnl: float = 0.0
    trades_opened: int = 0

    def loss_fraction(self, current_equity: float) -> float:
        """Positive number = fraction of start-of-day equity lost."""
        return max(0.0, (self.start_equity - current_equity) / self.start_equity)


@dataclass
class LimitBook:
    correlation_clusters: dict[str, str] = field(default_factory=dict)  # symbol -> cluster id
    _daily: DailyState | None = field(default=None, repr=False)
    _cooldowns: dict[str, datetime] = field(default_factory=dict, repr=False)
    _last_direction: dict[str, str] = field(default_factory=dict, repr=False)

    # -- day roll ---------------------------------------------------- #
    def roll_day(self, now: datetime, equity: float) -> None:
        d = now.date()
        if self._daily is None or self._daily.day != d:
            self._daily = DailyState(day=d, start_equity=equity)

    @property
    def daily(self) -> DailyState:
        if self._daily is None:
            raise RuntimeError("roll_day has not been called")
        return self._daily

    # -- updates from fills ---------------------------------------- #
    def on_open(self, symbol: str, direction: str, now: datetime) -> None:
        self.daily.trades_opened += 1
        self._last_direction[symbol] = direction

    def on_realised(self, pnl: float) -> None:
        self.daily.realised_pnl += pnl

    def on_stop_out(self, symbol: str, now: datetime, cooldown: timedelta) -> None:
        self._cooldowns[symbol] = now + cooldown

    # -- queries used by gates ----------------------------------- #
    def in_cooldown(self, symbol: str, now: datetime) -> bool:
        until = self._cooldowns.get(symbol)
        return until is not None and now < until

    def cooldown_until(self, symbol: str) -> datetime | None:
        return self._cooldowns.get(symbol)

    def last_direction(self, symbol: str) -> str | None:
        return self._last_direction.get(symbol)

    def cluster_of(self, symbol: str) -> str:
        return self.correlation_clusters.get(symbol, symbol)

    def cluster_symbols(self, cluster: str) -> list[str]:
        return [s for s, c in self.correlation_clusters.items() if c == cluster]
