"""Kill switches - three layers (design section 7).

1. Manual     - one command, or a file on disk the engine polls.
2. Automatic  - daily loss limit, reconciliation drift, feed outage, repeated
                rejections.
3. Broker-side - a hard account limit set with the broker, outside this code.
                 (Not implementable here - it must not depend on our code. This
                 module only records that it should exist.)

Semantics: STOP OPENING, KEEP MANAGING. A halt must never orphan an open
position. Because brackets are server-side at entry, existing positions stay
protected even if this process is dead.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(slots=True)
class KillSwitchState:
    engaged: bool
    reason: str
    since: datetime | None


class KillSwitch:
    def __init__(
        self,
        flag_file: str | Path | None = None,
        *,
        daily_loss_limit: float = 0.03,
        max_rejections_per_window: int = 5,
        rejection_window_s: float = 300.0,
        feed_stale_after_s: float = 180.0,
    ) -> None:
        self.flag_file = Path(flag_file) if flag_file else None
        self.daily_loss_limit = daily_loss_limit
        self.max_rejections = max_rejections_per_window
        self.rejection_window_s = rejection_window_s
        self.feed_stale_after_s = feed_stale_after_s

        self._manual = False
        self._auto_reason = ""
        self._since: datetime | None = None
        self._rejections: deque[float] = deque()
        self._last_feed_ts: dict[str, float] = {}

    # -- manual layer ------------------------------------------------ #
    def engage(self, reason: str = "manual") -> None:
        self._manual = True
        self._auto_reason = self._auto_reason or reason
        self._since = self._since or datetime.now(UTC)

    def release(self) -> None:
        self._manual = False
        self._auto_reason = ""
        self._since = None
        self._rejections.clear()

    # -- automatic triggers -------------------------------------- #
    def on_rejection(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self._rejections.append(now)
        while self._rejections and now - self._rejections[0] > self.rejection_window_s:
            self._rejections.popleft()
        if len(self._rejections) >= self.max_rejections:
            self._trip(f"{len(self._rejections)} rejections in {self.rejection_window_s:.0f}s")

    def on_daily_loss(self, loss_fraction: float) -> None:
        if loss_fraction >= self.daily_loss_limit:
            self._trip(f"daily loss {loss_fraction:.2%} >= limit {self.daily_loss_limit:.2%}")

    def on_reconcile_divergence(self, detail: str) -> None:
        self._trip(f"reconciliation drift: {detail}")

    def on_feed_tick(self, source: str, now: float | None = None) -> None:
        self._last_feed_ts[source] = now if now is not None else time.time()

    def check_feed(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        for src, ts in self._last_feed_ts.items():
            if now - ts > self.feed_stale_after_s:
                self._trip(f"feed '{src}' stale for {now - ts:.0f}s")

    def _trip(self, reason: str) -> None:
        if not self._auto_reason:
            self._auto_reason = reason
            self._since = datetime.now(UTC)

    # -- query ------------------------------------------------- #
    @property
    def engaged(self) -> bool:
        return self._manual or bool(self._auto_reason) or self._file_engaged()

    def _file_engaged(self) -> bool:
        return self.flag_file is not None and self.flag_file.exists()

    def state(self) -> KillSwitchState:
        reason = self._auto_reason
        if self._file_engaged() and not reason:
            reason = f"flag file present: {self.flag_file}"
        if self._manual and not reason:
            reason = "manual"
        return KillSwitchState(engaged=self.engaged, reason=reason, since=self._since)

    def __call__(self) -> bool:
        """So it can be passed straight to ``RiskManager(kill_switch=...)``."""
        return self.engaged
