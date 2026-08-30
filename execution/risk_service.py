"""Bus adapter around ``risk.RiskManager``: subscribes to ``Signal``, publishes
``Order`` or ``Rejection``. This is the only path from intent to an executable
order, in both backtest and live.
"""

from __future__ import annotations

from collections.abc import Callable

from core.bus import EventBus
from core.events import Fill, Signal
from risk.manager import MarketState, RiskManager

MarketStateFn = Callable[[str], MarketState]


class RiskService:
    def __init__(
        self,
        bus: EventBus,
        manager: RiskManager,
        ctx,
        market_state_fn: MarketStateFn,
    ) -> None:
        self.bus = bus
        self.manager = manager
        self.ctx = ctx
        self._market_state_fn = market_state_fn
        self.decisions: list = []
        bus.subscribe(Signal, self._on_signal)
        bus.subscribe(Fill, self._on_fill)

    async def _on_signal(self, sig: Signal) -> None:
        market = self._market_state_fn(sig.symbol)
        decision = self.manager.evaluate(sig, self.ctx, market)
        self.decisions.append(decision)
        if decision.order is not None:
            await self.bus.publish(decision.order)
        elif decision.rejection is not None:
            await self.bus.publish(decision.rejection)

    async def _on_fill(self, fill: Fill) -> None:
        # keep the limit book's day/trade bookkeeping in step with reality
        lb = self.manager.limits
        if fill.is_partial:
            return
        lb.roll_day(fill.ts, self.ctx.equity)
        lb.on_open(fill.symbol, "long" if fill.side.value == "buy" else "short", fill.ts)
