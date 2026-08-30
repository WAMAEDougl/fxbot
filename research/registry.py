"""Strategy Registry (design section 2).

The only artifact that crosses from research into the execution plane: an
approved, versioned strategy config with provenance. Stored as signed JSON.

Provenance recorded:
* strategy class + config (the exact params)
* the validation report that justified promotion (DSR, PBO, CPCV paths, trials)
* code fingerprint: feature versions + git commit
* who approved it and when
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    deflated_sharpe: float
    pbo: float
    n_trials: int
    cpcv_min_expectancy: float
    worst_path_max_dd: float
    n_trades: int
    hypothesis: str
    notes: str = ""

    def passes(self, *, min_dsr: float = 0.95, max_pbo: float = 0.5, min_trades: int = 100) -> bool:
        return (
            self.deflated_sharpe > min_dsr
            and self.pbo < max_pbo
            and self.cpcv_min_expectancy > 0
            and self.n_trades >= min_trades
        )


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    strategy_id: str
    strategy_class: str  # import path, e.g. "strategies.ma_crossover:MaCrossover"
    config: dict[str, Any]
    report: ValidationReport
    feature_versions: dict[str, str]
    git_commit: str
    approved_by: str
    approved_at: str
    stage: str = "paper"  # paper -> canary -> scaled
    signature: str = ""

    def payload_hash(self) -> str:
        body = {
            "strategy_id": self.strategy_id,
            "strategy_class": self.strategy_class,
            "config": self.config,
            "report": asdict(self.report),
            "feature_versions": self.feature_versions,
            "git_commit": self.git_commit,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "stage": self.stage,
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class StrategyRegistry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def promote(
        self,
        *,
        strategy_id: str,
        strategy_class: str,
        config: dict,
        report: ValidationReport,
        feature_versions: dict[str, str],
        approved_by: str,
        secret: str,
        stage: str = "paper",
    ) -> RegistryEntry:
        if not report.passes():
            raise ValueError(
                f"promotion refused: report fails criteria "
                f"(DSR={report.deflated_sharpe}, PBO={report.pbo}, "
                f"trades={report.n_trades}, cpcv_min_exp={report.cpcv_min_expectancy})"
            )
        entry = RegistryEntry(
            strategy_id=strategy_id,
            strategy_class=strategy_class,
            config=config,
            report=report,
            feature_versions=feature_versions,
            git_commit=_git_commit(),
            approved_by=approved_by,
            approved_at=datetime.now(UTC).isoformat(),
            stage=stage,
        )
        sig = _sign(entry.payload_hash(), secret)
        entry = _replace_signature(entry, sig)
        path = self.root / f"{strategy_id}.json"
        path.write_text(json.dumps(_entry_to_json(entry), indent=2))
        return entry

    def load(self, strategy_id: str, *, secret: str) -> RegistryEntry:
        path = self.root / f"{strategy_id}.json"
        entry = _entry_from_json(json.loads(path.read_text()))
        expected = _sign(entry.payload_hash(), secret)
        if not _consteq(expected, entry.signature):
            raise ValueError(f"registry entry {strategy_id} failed signature check - tampered")
        return entry

    def list_active(self, *, secret: str) -> list[RegistryEntry]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(self.load(p.stem, secret=secret))
            except ValueError:
                continue
        return out


# -- signing (HMAC; swap for asymmetric signing in production) ------------- #
def _sign(payload_hash: str, secret: str) -> str:
    import hmac

    return hmac.new(secret.encode(), payload_hash.encode(), hashlib.sha256).hexdigest()


def _consteq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a, b)


def _replace_signature(e: RegistryEntry, sig: str) -> RegistryEntry:
    d = _entry_to_json(e)
    d["signature"] = sig
    return _entry_from_json(d)


def _entry_to_json(e: RegistryEntry) -> dict:
    d = asdict(e)
    return d


def _entry_from_json(d: dict) -> RegistryEntry:
    d = dict(d)
    d["report"] = ValidationReport(**d["report"])
    return RegistryEntry(**d)
