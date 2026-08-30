"""Result of a single risk gate. Lives in core so both risk/ and the backtester
can depend on it without importing the whole risk package."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GateAction(str, Enum):
    PASS = "pass"
    CLIP = "clip"  # allowed, but with modified size / geometry
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class GateResult:
    action: GateAction
    code: str = ""
    detail: str = ""
    # multiplicative adjustments a CLIP gate can request; combined by the manager
    size_mult: float = 1.0
    stop_mult: float = 1.0
    meta: dict = field(default_factory=dict)

    @classmethod
    def ok(cls) -> GateResult:
        return cls(action=GateAction.PASS)

    @classmethod
    def block(cls, code: str, detail: str = "") -> GateResult:
        return cls(action=GateAction.BLOCK, code=code, detail=detail)

    @classmethod
    def clip(
        cls, code: str, *, size_mult: float = 1.0, stop_mult: float = 1.0, detail: str = ""
    ) -> GateResult:
        return cls(
            action=GateAction.CLIP,
            code=code,
            size_mult=size_mult,
            stop_mult=stop_mult,
            detail=detail,
        )
