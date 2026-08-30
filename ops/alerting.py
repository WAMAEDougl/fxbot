"""Alerting with severity tiers (design section 9). SCAFFOLD - build order step 7.

You need to be woken up for a kill-switch trip, not for a fill. Transports are
pluggable; a console transport is provided so the wiring is testable offline.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from core.bus import EventBus
from core.events import Heartbeat, Rejection


class Severity(enum.IntEnum):
    INFO = 10
    WARNING = 20
    CRITICAL = 30  # wake a human


@dataclass(frozen=True, slots=True)
class Alert:
    severity: Severity
    title: str
    body: str
    ts: datetime


Transport = Callable[[Alert], None]


def console_transport(alert: Alert) -> None:
    print(f"[{alert.severity.name}] {alert.ts.isoformat()} {alert.title}: {alert.body}")


class Alerter:
    def __init__(self, transports: dict[Severity, list[Transport]] | None = None) -> None:
        self.transports = transports or {s: [console_transport] for s in Severity}

    def send(self, severity: Severity, title: str, body: str = "") -> None:
        alert = Alert(severity, title, body, datetime.now(UTC))
        for t in self.transports.get(severity, []):
            try:
                t(alert)
            except Exception as exc:  # noqa: BLE001 - alerting must not crash the engine
                print(f"alert transport failed: {exc!r}")

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(Rejection, self._on_rejection)
        bus.subscribe(Heartbeat, self._on_heartbeat)

    def _on_rejection(self, e: Rejection) -> None:
        sev = Severity.CRITICAL if e.reason_code in _CRITICAL_CODES else Severity.WARNING
        self.send(sev, f"Rejection {e.reason_code}", f"{e.origin}: {e.detail} ({e.symbol})")

    def _on_heartbeat(self, e: Heartbeat) -> None:
        if e.state != "ok":
            self.send(Severity.CRITICAL, f"Component {e.component_id} {e.state}", e.detail)


_CRITICAL_CODES = {
    "KILL_SWITCH",
    "DAILY_LOSS_LIMIT",
    "STALE_DATA",
    "RECONCILE_DRIFT",
    "INSUFFICIENT_MARGIN",
}


def telegram_transport(bot_token: str, chat_id: str) -> Transport:  # pragma: no cover
    """Returns a transport that posts to Telegram. Requires network + creds."""
    import urllib.request

    def _send(alert: Alert) -> None:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = json.dumps(
            {"chat_id": chat_id, "text": f"[{alert.severity.name}] {alert.title}\n{alert.body}"}
        ).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)

    return _send
