"""Write-ahead persistence for portfolio + OMS state (design 4.6).

SCAFFOLD - build order step 6/7. A process restart must not lose position
state, and a crash mid-order must be recoverable. This is the minimal
SQLite-backed journal: state changes are appended before they are acted on.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,          -- 'position' | 'order' | 'fill' | 'snapshot'
    key        TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_kind_key ON journal(kind, key);
"""


class StateJournal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(self.path, isolation_level=None)  # autocommit
        self._con.executescript(_SCHEMA)

    def append(self, kind: str, key: str, payload: dict) -> None:
        self._con.execute(
            "INSERT INTO journal(ts, kind, key, payload) VALUES (?, ?, ?, ?)",
            (datetime.now(UTC).isoformat(), kind, key, json.dumps(payload, default=str)),
        )

    def latest(self, kind: str, key: str) -> dict | None:
        row = self._con.execute(
            "SELECT payload FROM journal WHERE kind=? AND key=? ORDER BY seq DESC LIMIT 1",
            (kind, key),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def replay(self, kind: str | None = None):
        q = "SELECT ts, kind, key, payload FROM journal"
        args: tuple = ()
        if kind:
            q += " WHERE kind=?"
            args = (kind,)
        q += " ORDER BY seq ASC"
        for ts, k, key, payload in self._con.execute(q, args):
            yield {"ts": ts, "kind": k, "key": key, "payload": json.loads(payload)}

    def close(self) -> None:
        self._con.close()
