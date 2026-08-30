"""Position sizing math (design 4.4).

    risk_amount   = equity * risk_fraction        # 0.5% default, 1% max standard, never > 2%
    stop_distance = |entry - stop|                # price terms
    size          = risk_amount / (stop_distance * value_per_point)
    size          = floor_to_lot_step(size)

Size is returned in *units of the instrument*, but it is derived from an amount
of account-currency risk. "0.1 lots" is never an input here - it is meaningless
across instruments.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

RISK_FRACTION_HARD_CAP = 0.02  # never exceed 2%, no matter the config


@dataclass(frozen=True, slots=True)
class SizingParams:
    value_per_point: float = 1.0  # account-ccy P/L per 1.0 price move per unit
    lot_step: float = 1.0  # smallest tradable increment
    min_lot: float = 1.0
    max_lot: float | None = None


@dataclass(frozen=True, slots=True)
class SizingResult:
    size: float
    risk_amount: float
    stop_distance: float
    risk_fraction_used: float
    capped: bool


def floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.floor(x / step + 1e-9) * step


def size_from_risk(
    *,
    equity: float,
    entry: float,
    stop: float,
    risk_fraction: float,
    params: SizingParams,
    conviction: float = 1.0,
) -> SizingResult:
    if equity <= 0:
        raise ValueError("equity must be positive")
    if risk_fraction <= 0:
        raise ValueError("risk_fraction must be positive")

    capped = risk_fraction > RISK_FRACTION_HARD_CAP
    rf = min(risk_fraction, RISK_FRACTION_HARD_CAP)
    # conviction scales *down* only; it never increases risk above the configured level
    rf_eff = rf * max(0.0, min(1.0, conviction))

    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        raise ValueError("stop distance is zero - entry == stop")

    risk_amount = equity * rf_eff
    raw = risk_amount / (stop_distance * params.value_per_point)
    size = floor_to_step(raw, params.lot_step)

    if params.max_lot is not None:
        size = min(size, params.max_lot)
    if size < params.min_lot:
        size = 0.0  # too small to trade within budget - caller rejects

    return SizingResult(
        size=size,
        risk_amount=risk_amount,
        stop_distance=stop_distance,
        risk_fraction_used=rf_eff,
        capped=capped,
    )


def realised_risk(size: float, entry: float, stop: float, value_per_point: float) -> float:
    """Account-ccy loss if the stop is hit exactly. Used by property tests."""
    return size * abs(entry - stop) * value_per_point
