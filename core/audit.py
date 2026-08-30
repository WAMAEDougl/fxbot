"""Append-only audit log (design 4.7).

Every event on the bus, in order, with timestamps. Not optional, not just for
debugging: it is the only way to tell bad edge from bad execution from a bug -
you replay the tape.

``JsonlAudit`` writes one JSON object per line; cheap, greppable, survives a
crash mid-write (the partial last line is discarded on read). ``ParquetAudit``
is the compacted long-term form.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from core.bus import EventBus
from core.events import Event


def _encode(ev: Event) -> dict[str, Any]:
    d = {"_type": type(ev).__name__}
    for f in dataclasses.fields(ev):
        v = getattr(ev, f.name)
        if isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, dict):
            v = {k: (x.isoformat() if isinstance(x, datetime) else x) for k, x in v.items()}
        elif hasattr(v, "value"):  # enum
            v = v.value
        d[f.name] = v
    return d


class JsonlAudit:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self.count = 0

    def record(self, ev: Event) -> None:
        self._fh.write(json.dumps(_encode(ev), separators=(",", ":")) + "\n")
        self._fh.flush()
        self.count += 1

    def attach(self, bus: EventBus) -> None:
        bus.subscribe_all(self.record)

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> JsonlAudit:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def read(path: str | Path) -> list[dict]:
        out = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                break  # partial final line after a crash
        return out


class MemoryAudit:
    """In-memory audit for tests and replay-determinism checks."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def record(self, ev: Event) -> None:
        self.events.append(ev)

    def attach(self, bus: EventBus) -> None:
        bus.subscribe_all(self.record)

    def digest(self) -> str:
        import hashlib

        h = hashlib.sha1()
        for ev in self.events:
            h.update(json.dumps(_encode(ev), sort_keys=True).encode())
        return h.hexdigest()
