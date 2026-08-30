from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class MaCrossoverConfig(BaseModel):
    """Versioned config. Promoted configs are signed and stored in the registry."""

    model_config = {"frozen": True, "extra": "forbid"}

    strategy_id: str = "ma_crossover"
    symbols: list[str] = Field(default_factory=list)
    fast: int = Field(20, ge=2)
    slow: int = Field(50, ge=3)
    atr_window: int = Field(14, ge=2)
    stop_atr_mult: float = Field(2.0, gt=0)
    target_rr: float = Field(2.0, gt=0)
    allow_short: bool = True
    conviction: float = Field(1.0, ge=0.0, le=1.0)
    seed: int = 0

    @model_validator(mode="after")
    def _fast_lt_slow(self) -> MaCrossoverConfig:
        if self.fast >= self.slow:
            raise ValueError("fast window must be < slow window")
        return self

    @property
    def warmup(self) -> int:
        return max(self.slow, self.atr_window) + 2
