from __future__ import annotations

from pydantic import BaseModel, Field


class EtfReversionConfig(BaseModel):
    """Short-term mean reversion in broad equity ETFs (research/hypotheses/etf_reversion.md).
    Few parameters on purpose: each one inflates the trial count the deflated Sharpe pays for."""

    model_config = {"frozen": True, "extra": "forbid"}

    strategy_id: str = "etf_reversion"
    symbols: list[str] = Field(default_factory=list)
    trend_window: int = Field(200, ge=20)  # only buy dips while above this average
    rsi_window: int = Field(2, ge=2)
    rsi_entry: float = Field(10.0, gt=0, lt=50)  # buy when RSI falls below this
    exit_window: int = Field(5, ge=2)  # exit when the close recovers above this average
    max_hold_bars: int = Field(10, ge=1)  # time stop
    atr_window: int = Field(14, ge=2)
    stop_atr_mult: float = Field(3.0, gt=0)  # wide disaster stop; exits are rule-based
    target_rr: float = Field(10.0, gt=0)  # distant target (a bracket is mandatory)
    allow_short: bool = False  # a priori: shorting overbought indices fights equity drift
    conviction: float = Field(1.0, ge=0.0, le=1.0)
    resignal_gap_bars: int = Field(3, ge=1)  # don't re-signal while an order may be in flight
    seed: int = 0

    @property
    def warmup(self) -> int:
        return max(self.trend_window, self.atr_window, self.exit_window) + 2
