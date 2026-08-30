"""Strategy engine (design 4.3). Hosts N strategies behind an exception boundary.

A crashing strategy is disabled and an alert is emitted; it never takes down the
engine. Strategies receive a ``ReadOnlyContext`` and emit ``Signal``s onto the
bus - they have no other outputs.
"""

from __future__ import annotations

from collections.abc import Callable

from core.bus import EventBus
from core.events import Fill, Heartbeat, MarketData, Signal
from core.protocols import ReadOnlyContext, Strategy


class StrategyEngine:
    def __init__(
        self,
        bus: EventBus,
        ctx: ReadOnlyContext,
        strategies: list[Strategy],
        *,
        on_disable: Callable[[str, BaseException], None] | None = None,
    ) -> None:
        self.bus = bus
        self.ctx = ctx
        self.strategies: dict[str, Strategy] = {s.strategy_id: s for s in strategies}
        self.enabled: dict[str, bool] = {sid: True for sid in self.strategies}
        self._on_disable = on_disable
        bus.subscribe(MarketData, self._on_market_data)
        bus.subscribe(Fill, self._on_fill)

    def enable(self, strategy_id: str) -> None:
        self.enabled[strategy_id] = True

    def disable(self, strategy_id: str, reason: str = "manual") -> None:
        self.enabled[strategy_id] = False

    async def _on_market_data(self, event: MarketData) -> None:
        for sid, strat in self.strategies.items():
            if not self.enabled.get(sid):
                continue
            try:
                signals = strat.on_market_data(event, self.ctx) or []
            except BaseException as exc:  # noqa: BLE001 - sandbox boundary
                self.enabled[sid] = False
                if self._on_disable:
                    self._on_disable(sid, exc)
                await self.bus.publish(
                    Heartbeat(
                        ts=event.ts,
                        component_id=f"strategy:{sid}",
                        state="down",
                        detail=f"disabled after exception: {exc!r}",
                    )
                )
                continue
            for sig in signals:
                _validate_signal(sig, sid)
                await self.bus.publish(sig)

    async def _on_fill(self, event: Fill) -> None:
        strat = self.strategies.get(event.strategy_id)
        if strat is None or not self.enabled.get(event.strategy_id):
            return
        try:
            strat.on_fill(event)
        except BaseException as exc:  # noqa: BLE001
            self.enabled[event.strategy_id] = False
            if self._on_disable:
                self._on_disable(event.strategy_id, exc)


def _validate_signal(sig: Signal, sid: str) -> None:
    if sig.strategy_id != sid:
        raise ValueError(f"strategy {sid} emitted a signal tagged {sig.strategy_id!r}")
    if not isinstance(sig, Signal):
        raise TypeError(f"strategy {sid} emitted a non-Signal: {type(sig)}")
