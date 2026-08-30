"""The risk manager. Turns a ``Signal`` into an executable ``Order`` or a
``Rejection`` - and nothing else in the system is allowed to build an ``Order``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from core.events import (
    Direction,
    Order,
    OrderType,
    Rejection,
    Side,
    Signal,
    TimeInForce,
)
from core.gate_result import GateAction, GateResult
from risk.gates import ORDERED_GATES, GateInput
from risk.limits import LimitBook
from risk.sizing import SizingParams, size_from_risk


@dataclass(slots=True)
class RiskConfig:
    # gate defaults (design 4.4 table)
    daily_loss_limit: float = 0.03
    max_trades_per_day: int = 4
    cooldown: timedelta = timedelta(minutes=30)
    min_rr: float = 1.5
    target_rr: float = 2.0
    correlation_cap_mult: float = 1.5
    stale_data_bars: float = 3.0
    min_stop_atr_mult: float = 1.0
    min_stop_spread_mult: float = 3.0
    event_size_mult: float = 0.5
    event_stop_mult: float = 1.5
    # sizing
    risk_fraction: float = 0.005  # 0.5% default
    max_risk_fraction: float = 0.01  # 1% standard max
    default_stop_atr_mult: float = 2.0
    order_ttl: timedelta = timedelta(days=1)


@dataclass(slots=True)
class MarketState:
    """What the manager needs to know about a symbol right now."""

    last_bar_ts: datetime | None = None
    bar_interval: timedelta = timedelta(days=1)
    spread: float | None = None
    atr: float | None = None
    price: float = 0.0
    data_gap_open: bool = False
    value_per_point: float = 1.0
    sizing: SizingParams = field(default_factory=SizingParams)
    margin_available: float = 1e12
    margin_rate: float = 1.0  # fraction of notional required as margin
    high_impact_event: bool = False


@dataclass(slots=True)
class RiskDecision:
    order: Order | None
    rejection: Rejection | None
    gate_trace: list[GateResult]

    @property
    def approved(self) -> bool:
        return self.order is not None


class RiskManager:
    def __init__(
        self,
        cfg: RiskConfig | None = None,
        *,
        limits: LimitBook | None = None,
        kill_switch=lambda: False,
    ) -> None:
        self.cfg = cfg or RiskConfig()
        self.limits = limits or LimitBook()
        self._kill_switch = kill_switch

    # ---------------------------------------------------------------- #
    def evaluate(self, signal: Signal, ctx, market: MarketState) -> RiskDecision:
        self.limits.roll_day(ctx.now, ctx.equity)
        entry, stop, target, side = self._geometry(signal, ctx, market)

        if side is None:  # FLAT / no-op
            return RiskDecision(None, self._reject(signal, "NO_OP", "flat signal, nothing to do"), [])

        # provisional size for risk-based gates (correlation, margin)
        prelim = self._size(signal, ctx, market, entry, stop)
        single_risk = prelim.size * abs(entry - stop) * market.value_per_point
        margin_required = prelim.size * market.price * market.value_per_point * market.margin_rate

        g = GateInput(
            signal=signal,
            now=ctx.now,
            equity=ctx.equity,
            position=ctx.position(signal.symbol),
            last_bar_ts=market.last_bar_ts,
            bar_interval=market.bar_interval,
            spread=market.spread,
            atr=market.atr,
            data_gap_open=market.data_gap_open,
            entry=entry,
            stop=stop,
            target=target,
            correlated_risk=self._cluster_risk(signal.symbol, ctx, market),
            single_trade_risk=single_risk,
            margin_available=market.margin_available,
            margin_required=margin_required,
            kill_switch=self._kill_switch(),
            high_impact_event=market.high_impact_event,
            limits=self.limits,
            cfg=self.cfg,
        )

        trace: list[GateResult] = []
        size_mult = 1.0
        stop_mult = 1.0
        for gate in ORDERED_GATES:
            res = gate(g)
            trace.append(res)
            if res.action is GateAction.BLOCK:
                return RiskDecision(None, self._reject(signal, res.code, res.detail), trace)
            if res.action is GateAction.CLIP:
                size_mult *= res.size_mult
                stop_mult *= res.stop_mult

        # apply clips to geometry, then re-size on the final stop
        if stop_mult != 1.0:
            stop = entry - (entry - stop) * stop_mult
        final = self._size(signal, ctx, market, entry, stop)
        size = final.size * size_mult
        size = _floor(size, market.sizing.lot_step)
        if size < market.sizing.min_lot or size <= 0:
            return RiskDecision(
                None, self._reject(signal, "SIZE_ZERO", "risk budget yields sub-minimum size"), trace
            )

        order = Order(
            ts=ctx.now,
            client_order_id=self._coid(signal),
            symbol=signal.symbol,
            side=side,
            qty=size,
            order_type=OrderType.MARKET,
            limit_price=None,
            stop_loss=round(stop, 8),
            take_profit=round(target, 8),
            tif=TimeInForce.GTD,
            ttl=ctx.now + self.cfg.order_ttl,
            strategy_id=signal.strategy_id,
            signal_id=signal.event_id,
            features_hash=signal.features_hash,
        )
        return RiskDecision(order, None, trace)

    # ---------------------------------------------------------------- #
    def _geometry(self, signal: Signal, ctx, market: MarketState):
        if signal.direction is Direction.FLAT:
            return 0.0, 0.0, 0.0, None
        price = signal.entry_hint or market.price
        atr = market.atr or (price * 0.01)
        long = signal.direction is Direction.LONG
        side = Side.BUY if long else Side.SELL

        if signal.stop_hint is not None:
            stop = signal.stop_hint
        else:
            d = self.cfg.default_stop_atr_mult * atr
            stop = price - d if long else price + d

        if signal.target_hint is not None:
            target = signal.target_hint
        else:
            r = abs(price - stop) * self.cfg.target_rr
            target = price + r if long else price - r
        return price, stop, target, side

    def _size(self, signal, ctx, market: MarketState, entry, stop):
        return size_from_risk(
            equity=ctx.equity,
            entry=entry,
            stop=stop,
            risk_fraction=min(self.cfg.risk_fraction, self.cfg.max_risk_fraction),
            params=SizingParams(
                value_per_point=market.value_per_point,
                lot_step=market.sizing.lot_step,
                min_lot=market.sizing.min_lot,
                max_lot=market.sizing.max_lot,
            ),
            conviction=signal.conviction,
        )

    def _cluster_risk(self, symbol: str, ctx, market: MarketState) -> float:
        cluster = self.limits.cluster_of(symbol)
        total = 0.0
        for other in self.limits.cluster_symbols(cluster):
            if other == symbol:
                continue
            pos = ctx.position(other)
            avg = ctx.avg_price(other)
            if pos and avg:
                # rough: assume same relative stop distance as this trade
                total += abs(pos) * abs(avg) * 0.01 * market.value_per_point
        return total

    def _reject(self, signal: Signal, code: str, detail: str) -> Rejection:
        return Rejection(
            ts=signal.ts,
            reason_code=code,
            origin="risk",
            originating_event_id=signal.event_id,
            detail=detail,
            symbol=signal.symbol,
            strategy_id=signal.strategy_id,
        )

    @staticmethod
    def _coid(signal: Signal) -> str:
        raw = f"{signal.strategy_id}|{signal.symbol}|{signal.ts.isoformat()}|{signal.event_id}"
        return hashlib.sha1(raw.encode()).hexdigest()[:24]


def _floor(x: float, step: float) -> float:
    import math

    if step <= 0:
        return x
    return math.floor(x / step + 1e-9) * step
