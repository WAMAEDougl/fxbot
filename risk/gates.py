"""The 11 pre-trade checks (design 4.4), each individually testable.

Gates run fail-fast in the numbered order. A gate returns a ``GateResult``:
PASS, BLOCK (with a rejection code), or CLIP (size/stop multipliers the manager
combines). The table of defaults lives in ``RiskConfig`` (risk/manager.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from core.events import Direction, Signal
from core.gate_result import GateResult
from risk.limits import LimitBook


@dataclass(slots=True)
class GateInput:
    signal: Signal
    now: datetime
    equity: float
    position: float  # signed, current
    # market state for this symbol
    last_bar_ts: datetime | None
    bar_interval: timedelta
    spread: float | None
    atr: float | None
    data_gap_open: bool
    # geometry (post strategy hint / defaults)
    entry: float
    stop: float
    target: float
    # portfolio
    correlated_risk: float  # sum of open risk in the same cluster, account ccy
    single_trade_risk: float  # this trade's risk, account ccy
    margin_available: float
    margin_required: float
    # external
    kill_switch: bool
    high_impact_event: bool
    limits: LimitBook
    cfg: RiskConfigProto


class RiskConfigProto:  # structural hint; real one in manager.py
    daily_loss_limit: float
    max_trades_per_day: int
    cooldown: timedelta
    min_rr: float
    target_rr: float
    correlation_cap_mult: float
    stale_data_bars: float
    min_stop_atr_mult: float
    min_stop_spread_mult: float
    event_size_mult: float
    event_stop_mult: float


# --------------------------------------------------------------------- #
# 1. kill switch
def gate_kill_switch(g: GateInput) -> GateResult:
    if g.kill_switch:
        return GateResult.block("KILL_SWITCH", "kill switch engaged")
    return GateResult.ok()


# 2. data freshness
def gate_data_freshness(g: GateInput) -> GateResult:
    if g.data_gap_open:
        return GateResult.block("STALE_DATA", "open data gap for symbol")
    if g.last_bar_ts is None:
        return GateResult.block("STALE_DATA", "no bars seen")
    age = g.now - g.last_bar_ts
    if age > g.bar_interval * g.cfg.stale_data_bars:
        return GateResult.block(
            "STALE_DATA", f"last bar {age} old (> {g.cfg.stale_data_bars} intervals)"
        )
    return GateResult.ok()


# 3. daily loss limit
def gate_daily_loss(g: GateInput) -> GateResult:
    lost = g.limits.daily.loss_fraction(g.equity)
    if lost >= g.cfg.daily_loss_limit:
        return GateResult.block(
            "DAILY_LOSS_LIMIT", f"down {lost:.2%} >= {g.cfg.daily_loss_limit:.2%}"
        )
    return GateResult.ok()


# 4. max trades today
def gate_trade_cap(g: GateInput) -> GateResult:
    if g.limits.daily.trades_opened >= g.cfg.max_trades_per_day:
        return GateResult.block(
            "TRADE_CAP", f"{g.limits.daily.trades_opened} trades opened today"
        )
    return GateResult.ok()


# 5. symbol cooldown after stop-out
def gate_cooldown(g: GateInput) -> GateResult:
    if g.limits.in_cooldown(g.signal.symbol, g.now):
        until = g.limits.cooldown_until(g.signal.symbol)
        return GateResult.block("COOLDOWN", f"cooldown until {until}")
    return GateResult.ok()


# 6. direction flip on same symbol
def gate_reversal_block(g: GateInput) -> GateResult:
    if g.signal.direction is Direction.FLAT:
        return GateResult.ok()
    if g.position != 0.0:
        holding_long = g.position > 0
        want_long = g.signal.direction is Direction.LONG
        if holding_long != want_long:
            return GateResult.block("REVERSAL_BLOCK", "would flip an open position")
    last = g.limits.last_direction(g.signal.symbol)
    if last and last != g.signal.direction.value and g.limits.in_cooldown(g.signal.symbol, g.now):
        return GateResult.block("REVERSAL_BLOCK", "immediate reversal after exit")
    return GateResult.ok()


# 7. correlated exposure cap
def gate_correlation_cap(g: GateInput) -> GateResult:
    cap = g.cfg.correlation_cap_mult * g.single_trade_risk
    if g.correlated_risk + g.single_trade_risk > cap:
        return GateResult.block(
            "CORRELATION_CAP",
            f"cluster risk {g.correlated_risk + g.single_trade_risk:.2f} > {cap:.2f}",
        )
    return GateResult.ok()


# 8. stop distance >= 1x ATR and well above spread
def gate_noise_tight_stop(g: GateInput) -> GateResult:
    dist = abs(g.entry - g.stop)
    if g.atr is not None and dist < g.cfg.min_stop_atr_mult * g.atr:
        return GateResult.block(
            "NOISE_TIGHT_STOP", f"stop {dist:.5f} < {g.cfg.min_stop_atr_mult}x ATR {g.atr:.5f}"
        )
    if g.spread is not None and dist < g.cfg.min_stop_spread_mult * g.spread:
        return GateResult.block(
            "NOISE_TIGHT_STOP", f"stop {dist:.5f} < {g.cfg.min_stop_spread_mult}x spread"
        )
    return GateResult.ok()


# 9. reward:risk >= min
def gate_reward_risk(g: GateInput) -> GateResult:
    risk = abs(g.entry - g.stop)
    reward = abs(g.target - g.entry)
    if risk <= 0:
        return GateResult.block("INSUFFICIENT_RR", "zero risk distance")
    rr = reward / risk
    if rr < g.cfg.min_rr:
        return GateResult.block("INSUFFICIENT_RR", f"RR {rr:.2f} < {g.cfg.min_rr}")
    return GateResult.ok()


# 10. margin available with headroom
def gate_margin(g: GateInput) -> GateResult:
    if g.margin_required > g.margin_available:
        return GateResult.block(
            "INSUFFICIENT_MARGIN",
            f"need {g.margin_required:.2f} > available {g.margin_available:.2f}",
        )
    return GateResult.ok()


# 11. high-impact event window -> clip, not block
def gate_event_window(g: GateInput) -> GateResult:
    if g.high_impact_event:
        return GateResult.clip(
            "EVENT_WINDOW",
            size_mult=g.cfg.event_size_mult,
            stop_mult=g.cfg.event_stop_mult,
            detail="high-impact event window: size x0.5, stop x1.5",
        )
    return GateResult.ok()


ORDERED_GATES = [
    gate_kill_switch,
    gate_data_freshness,
    gate_daily_loss,
    gate_trade_cap,
    gate_cooldown,
    gate_reversal_block,
    gate_correlation_cap,
    gate_noise_tight_stop,
    gate_reward_risk,
    gate_margin,
    gate_event_window,
]
