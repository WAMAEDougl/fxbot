"""Simulated broker for the backtester.

Consumes ``Order`` events, applies latency, runs the ``FillSimulator`` against
the *next* bar, and manages server-side brackets (stop-loss / take-profit) for
open positions exactly as a real venue would - the bracket goes in *with* the
entry, so a position is never unprotected.

Same-bar ambiguity (both stop and target inside one bar's range) is resolved
pessimistically: assume the stop filled first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.bus import EventBus
from core.events import Fill, MarketData, Order, OrderType, Rejection, Side
from research.costs import CostModel, FillSimulator


@dataclass(slots=True)
class _Pending:
    order: Order
    bars_waited: int = 0


@dataclass(slots=True)
class _Bracket:
    symbol: str
    side: Side  # side of the *entry*
    qty: float
    entry_price: float
    stop: float
    target: float
    strategy_id: str
    opened_ts: datetime
    signal_id: str = ""


@dataclass(slots=True)
class TradeRecord:
    symbol: str
    strategy_id: str
    direction: str
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    fees: float
    r_multiple: float
    exit_reason: str


class BacktestBroker:
    broker = "sim"

    def __init__(
        self,
        bus: EventBus,
        model: CostModel,
        *,
        seed: int = 0,
        value_per_point: dict[str, float] | None = None,
        thin_session_hours: tuple[int, ...] = (21, 22, 23, 0),
    ) -> None:
        self.bus = bus
        self.model = model
        self.sim = FillSimulator(model, seed=seed)
        self.vpp = value_per_point or {}
        self.thin_hours = set(thin_session_hours)
        self._pending: list[_Pending] = []
        self._brackets: dict[str, _Bracket] = {}
        self.trades: list[TradeRecord] = []
        self.rejections: list[Rejection] = []
        bus.subscribe(Order, self._on_order)
        bus.subscribe(MarketData, self._on_market_data)

    # -- order intake --------------------------------------------- #
    async def _on_order(self, order: Order) -> None:
        self._pending.append(_Pending(order=order))

    # -- per-bar processing ------------------------------------- #
    async def _on_market_data(self, bar: MarketData) -> None:
        await self._process_brackets(bar)
        await self._process_pending(bar)

    async def _process_brackets(self, bar: MarketData) -> None:
        br = self._brackets.get(bar.symbol)
        if br is None:
            return
        hi = bar.high if bar.high is not None else bar.price
        lo = bar.low if bar.low is not None else bar.price
        exit_reason = ""
        exit_price = 0.0
        if br.side is Side.BUY:  # long: stop below, target above
            if lo <= br.stop:
                exit_price, exit_reason = br.stop, "STOP"
            elif hi >= br.target:
                exit_price, exit_reason = br.target, "TARGET"
        else:  # short
            if hi >= br.stop:
                exit_price, exit_reason = br.stop, "STOP"
            elif lo <= br.target:
                exit_price, exit_reason = br.target, "TARGET"
        if not exit_reason:
            return

        exit_side = Side.SELL if br.side is Side.BUY else Side.BUY
        thin = bar.ts.hour in self.thin_hours
        half_spread = self.model.spread_at(exit_price, thin_session=thin) / 2.0
        slip = -half_spread if exit_side is Side.SELL else half_spread
        fill_price = exit_price + slip
        fees = self.model.commission(fill_price * br.qty)

        await self.bus.publish(
            Fill(
                ts=bar.ts,
                order_id=f"bracket-{bar.symbol}-{exit_reason}",
                symbol=bar.symbol,
                side=exit_side,
                price=fill_price,
                qty=br.qty,
                fees=fees,
                strategy_id=br.strategy_id,
            )
        )

        vpp = self.vpp.get(bar.symbol, 1.0)
        direction_sign = 1.0 if br.side is Side.BUY else -1.0
        gross = (fill_price - br.entry_price) * direction_sign * br.qty * vpp
        risk_per_unit = abs(br.entry_price - br.stop) * vpp
        r_mult = gross / (risk_per_unit * br.qty) if risk_per_unit > 0 else 0.0
        self.trades.append(
            TradeRecord(
                symbol=bar.symbol,
                strategy_id=br.strategy_id,
                direction="long" if br.side is Side.BUY else "short",
                entry_ts=br.opened_ts,
                exit_ts=bar.ts,
                entry_price=br.entry_price,
                exit_price=fill_price,
                qty=br.qty,
                pnl=gross - fees,
                fees=fees,
                r_multiple=r_mult,
                exit_reason=exit_reason,
            )
        )
        del self._brackets[bar.symbol]

    async def _process_pending(self, bar: MarketData) -> None:
        still: list[_Pending] = []
        for p in self._pending:
            o = p.order
            if o.symbol != bar.symbol:
                still.append(p)
                continue
            if o.ttl is not None and bar.ts > o.ttl:
                rej = Rejection(
                    ts=bar.ts,
                    reason_code="EXPIRED",
                    origin="broker",
                    originating_event_id=o.event_id,
                    detail="order TTL elapsed before fill",
                    symbol=o.symbol,
                    strategy_id=o.strategy_id,
                )
                self.rejections.append(rej)
                await self.bus.publish(rej)
                continue
            p.bars_waited += 1
            if p.bars_waited < self.model.latency_bars:
                still.append(p)
                continue

            if o.symbol in self._brackets:
                # already have a position from an earlier order; skip (reversal
                # blocks should have caught this, but be safe)
                still.append(p)
                continue

            thin = bar.ts.hour in self.thin_hours
            ref = bar.open if bar.open is not None else bar.price
            if o.order_type is OrderType.LIMIT:
                out = self.sim.simulate_limit(
                    o, bar_high=bar.high or ref, bar_low=bar.low or ref,
                    bar_volume=bar.volume or 0.0, thin_session=thin,
                )
            else:
                out = self.sim.simulate_market(
                    o, ref_price=ref, bar_high=bar.high or ref, bar_low=bar.low or ref,
                    bar_volume=bar.volume or 0.0, thin_session=thin,
                )

            if out.rejected or out.filled_qty <= 0:
                rej = Rejection(
                    ts=bar.ts,
                    reason_code=out.reason or "NO_FILL",
                    origin="broker",
                    originating_event_id=o.event_id,
                    detail="simulator did not fill",
                    symbol=o.symbol,
                    strategy_id=o.strategy_id,
                )
                self.rejections.append(rej)
                await self.bus.publish(rej)
                if out.reason in ("NO_TOUCH", "QUEUE_NOT_CLEARED"):
                    still.append(_Pending(order=o, bars_waited=self.model.latency_bars))
                continue

            await self.bus.publish(
                Fill(
                    ts=bar.ts,
                    order_id=o.client_order_id,
                    symbol=o.symbol,
                    side=o.side,
                    price=out.fill_price,
                    qty=out.filled_qty,
                    fees=out.fees,
                    is_partial=out.is_partial,
                    strategy_id=o.strategy_id,
                )
            )
            self._brackets[o.symbol] = _Bracket(
                symbol=o.symbol,
                side=o.side,
                qty=out.filled_qty,
                entry_price=out.fill_price,
                stop=o.stop_loss,
                target=o.take_profit,
                strategy_id=o.strategy_id,
                opened_ts=bar.ts,
                signal_id=o.signal_id,
            )
            if out.is_partial:  # keep the remainder working one more bar
                pass
        self._pending = still

    # -- forced flatten at end of test ------------------------- #
    async def flatten_all(self, last_bars: dict[str, MarketData]) -> None:
        for symbol, br in list(self._brackets.items()):
            bar = last_bars.get(symbol)
            if bar is None:
                continue
            exit_side = Side.SELL if br.side is Side.BUY else Side.BUY
            price = bar.price
            fees = self.model.commission(price * br.qty)
            await self.bus.publish(
                Fill(ts=bar.ts, order_id=f"eod-flat-{symbol}", symbol=symbol,
                     side=exit_side, price=price, qty=br.qty, fees=fees,
                     strategy_id=br.strategy_id)
            )
            vpp = self.vpp.get(symbol, 1.0)
            sign = 1.0 if br.side is Side.BUY else -1.0
            gross = (price - br.entry_price) * sign * br.qty * vpp
            rpu = abs(br.entry_price - br.stop) * vpp
            self.trades.append(
                TradeRecord(
                    symbol=symbol, strategy_id=br.strategy_id,
                    direction="long" if br.side is Side.BUY else "short",
                    entry_ts=br.opened_ts, exit_ts=bar.ts,
                    entry_price=br.entry_price, exit_price=price, qty=br.qty,
                    pnl=gross - fees, fees=fees,
                    r_multiple=gross / (rpu * br.qty) if rpu > 0 else 0.0,
                    exit_reason="EOD_FLAT",
                )
            )
            del self._brackets[symbol]
