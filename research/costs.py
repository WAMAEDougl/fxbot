"""Execution-realism model for the backtester (design 5.2).

A backtest that does not model these is not evidence:

* spread at the actual time of day
* commission and financing / swap
* slippage as a function of order size vs available liquidity (not a constant)
* queue position for limit orders
* partial fills and rejections
* latency between signal and order arrival

Defaults here are deliberately pessimistic. Strategies that only work with
optimistic costs do not work.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from core.events import Order, Side


@dataclass(frozen=True, slots=True)
class CostModel:
    # --- spread ---------------------------------------------------- #
    spread_bps: float = 2.0  # baseline half-turn spread in bps of price
    rollover_spread_mult: float = 3.0  # widen near session rollover / thin hours
    # --- commission ---------------------------------------------- #
    commission_bps: float = 0.5  # per side, bps of notional
    commission_min: float = 0.0  # absolute floor per fill (account ccy)
    # --- financing / swap -------------------------------------- #
    swap_bps_per_day: float = 0.15  # carrying cost per day held, bps of notional
    # --- slippage (size vs liquidity) ------------------------- #
    impact_coef: float = 0.1  # price impact = impact_coef * sqrt(participation)
    participation_cap: float = 0.25  # max fraction of bar volume you can take
    # --- limit order queue ----------------------------------- #
    queue_fill_prob_at_touch: float = 0.35  # P(fill) when price merely touches limit
    queue_require_through: bool = True  # need price to trade *through* the limit
    # --- partial fills / rejection --------------------------- #
    reject_prob: float = 0.01
    partial_prob: float = 0.05
    # --- latency -------------------------------------------- #
    latency_bars: int = 1  # signal at bar t -> order works at bar t+latency_bars

    def spread_at(self, price: float, *, thin_session: bool = False) -> float:
        bps = self.spread_bps * (self.rollover_spread_mult if thin_session else 1.0)
        return price * bps / 10_000.0

    def commission(self, notional: float) -> float:
        return max(self.commission_min, abs(notional) * self.commission_bps / 10_000.0)

    def swap(self, notional: float, days_held: float) -> float:
        return abs(notional) * self.swap_bps_per_day / 10_000.0 * max(0.0, days_held)

    def slippage(self, price: float, qty: float, bar_volume: float, side: Side) -> float:
        """Adverse price move from taking liquidity. Always hurts."""
        if bar_volume <= 0:
            participation = self.participation_cap
        else:
            participation = min(self.participation_cap, abs(qty) / bar_volume)
        move = price * self.impact_coef * math.sqrt(participation) / 100.0
        return move if side is Side.BUY else -move

    def max_fillable(self, bar_volume: float) -> float:
        return bar_volume * self.participation_cap if bar_volume > 0 else float("inf")


@dataclass(frozen=True, slots=True)
class FillOutcome:
    filled_qty: float
    fill_price: float
    fees: float
    rejected: bool = False
    reason: str = ""
    is_partial: bool = False


class FillSimulator:
    def __init__(self, model: CostModel, *, seed: int = 0) -> None:
        self.m = model
        self.rng = np.random.default_rng(seed)

    def simulate_market(
        self,
        order: Order,
        *,
        ref_price: float,
        bar_high: float,
        bar_low: float,
        bar_volume: float,
        thin_session: bool = False,
    ) -> FillOutcome:
        if self.rng.random() < self.m.reject_prob:
            return FillOutcome(0.0, 0.0, 0.0, rejected=True, reason="SIM_REJECT")

        half_spread = self.m.spread_at(ref_price, thin_session=thin_session) / 2.0
        signed_spread = half_spread if order.side is Side.BUY else -half_spread
        slip = self.m.slippage(ref_price, order.qty, bar_volume, order.side)
        price = ref_price + signed_spread + slip
        price = min(max(price, bar_low), bar_high)  # cannot fill outside the bar

        qty = order.qty
        is_partial = False
        cap = self.m.max_fillable(bar_volume)
        if qty > cap or self.rng.random() < self.m.partial_prob:
            qty = min(qty, cap) if qty > cap else qty * float(self.rng.uniform(0.4, 0.9))
            qty = max(qty, 0.0)
            is_partial = qty < order.qty

        notional = price * qty
        fees = self.m.commission(notional)
        return FillOutcome(qty, price, fees, is_partial=is_partial)

    def simulate_limit(
        self,
        order: Order,
        *,
        bar_high: float,
        bar_low: float,
        bar_volume: float,
        thin_session: bool = False,
    ) -> FillOutcome:
        lp = order.limit_price
        assert lp is not None
        touched = bar_low <= lp <= bar_high
        through = (bar_low < lp) if order.side is Side.BUY else (bar_high > lp)
        if not touched:
            return FillOutcome(0.0, 0.0, 0.0, reason="NO_TOUCH")
        if self.m.queue_require_through and not through:
            if self.rng.random() > self.m.queue_fill_prob_at_touch:
                return FillOutcome(0.0, 0.0, 0.0, reason="QUEUE_NOT_CLEARED")

        qty = order.qty
        cap = self.m.max_fillable(bar_volume)
        is_partial = False
        if qty > cap:
            qty = cap
            is_partial = True
        # passive fill: no spread cost, tiny/zero slippage
        fees = self.m.commission(lp * qty)
        return FillOutcome(qty, lp, fees, is_partial=is_partial)
