"""Reconciliation loop (design 4.5).

SCAFFOLD - build order step 7. On startup and every N minutes, fetch the
broker's authoritative positions and compare to local state. Divergence beyond
tolerance -> alert + halt new orders. Local state is a cache; the broker is the
source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from core.clock import Clock
from core.protocols import BrokerAdapter
from execution.portfolio import Portfolio


@dataclass(slots=True)
class Divergence:
    symbol: str
    local_qty: float
    broker_qty: float

    @property
    def delta(self) -> float:
        return self.broker_qty - self.local_qty


@dataclass(slots=True)
class ReconcileResult:
    ts: datetime
    ok: bool
    divergences: list[Divergence] = field(default_factory=list)
    equity_delta: float = 0.0


class Reconciler:
    def __init__(
        self,
        broker: BrokerAdapter,
        portfolio: Portfolio,
        clock: Clock,
        *,
        qty_tolerance: float = 1e-6,
        equity_tolerance_frac: float = 0.005,
        on_divergence=None,
    ) -> None:
        self.broker = broker
        self.portfolio = portfolio
        self.clock = clock
        self.qty_tol = qty_tolerance
        self.equity_tol = equity_tolerance_frac
        self._on_divergence = on_divergence
        self.halted = False

    async def run_once(self) -> ReconcileResult:
        broker_pos = await self.broker.positions()
        broker_acct = await self.broker.account()

        symbols = set(broker_pos) | set(self.portfolio.open_symbols())
        divs: list[Divergence] = []
        for sym in symbols:
            bq = broker_pos.get(sym, 0.0)
            lq = self.portfolio.position(sym)
            if abs(bq - lq) > self.qty_tol:
                divs.append(Divergence(sym, lq, bq))

        eq_delta = broker_acct.get("equity", self.portfolio.equity) - self.portfolio.equity
        eq_bad = abs(eq_delta) > self.equity_tol * max(self.portfolio.equity, 1.0)

        ok = not divs and not eq_bad
        if not ok:
            self.halted = True
            if self._on_divergence:
                self._on_divergence(divs, eq_delta)
        return ReconcileResult(self.clock.now(), ok, divs, eq_delta)
