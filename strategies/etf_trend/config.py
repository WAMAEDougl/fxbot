from __future__ import annotations

from pydantic import BaseModel, Field


class EtfTrendConfig(BaseModel):
    """Time-series trend following on a diversified ETF basket (see
    research/hypotheses/etf_trend.md). Deliberately few parameters: every extra
    knob inflates the trial count that the deflated Sharpe must pay for."""

    model_config = {"frozen": True, "extra": "forbid"}

    strategy_id: str = "etf_trend"
    symbols: list[str] = Field(default_factory=list)
    lookback: int = Field(200, ge=20)  # SMA window defining the trend
    atr_window: int = Field(14, ge=2)
    stop_atr_mult: float = Field(3.0, gt=0)  # stop fixed at entry (disaster stop when exit_on_trend_flip)
    target_rr: float = Field(3.0, gt=0)  # distant target; the stop (or the trend-flip close) is the real exit
    allow_short: bool = True
    conviction: float = Field(1.0, ge=0.0, le=1.0)
    resignal_gap_bars: int = Field(3, ge=1)  # don't re-signal while an order may be in flight
    # Close the position when the trend flips against it (a reduce-only close order),
    # instead of waiting for the stop. The bracket then acts as a disaster stop only.
    exit_on_trend_flip: bool = False
    seed: int = 0

    @property
    def warmup(self) -> int:
        return max(self.lookback, self.atr_window) + 2
