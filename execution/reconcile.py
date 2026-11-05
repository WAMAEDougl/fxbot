"""Reconciliation loop (design 4.5).

SCAFFOLD - build order step 7. On startup and every N minutes, fetch the
broker's authoritative positions and compare to local state. Divergence beyond
tolerance -> alert + halt new orders. Local state is a cache; the broker is the
source of truth.

Money check: compares the broker's CASH with the cash the local fills imply, not equity.
Equity includes mark-to-market, and the local portfolio re-marks only once per daily bar
while the broker marks live, so a normal 1-3% day made "equity drift" a false alarm that
halted the bot (4 Nov 2026). Cash moves only on fills (and rare dividends), so it is stable.
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
    equity_delta: float = 0.0  # informational only (marks differ); never halts
    cash_delta: float = 0.0


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
        on_ok=None,
    ) -> None:
        self.broker = broker
        self.portfolio = portfolio
        self.clock = clock
        self.qty_tol = qty_tolerance
        self.equity_tol = equity_tolerance_frac
        self._on_divergence = on_divergence
        self._on_ok = on_ok
        self.halted = False
        self._cash_base: tuple[float, int] | None = None  # (broker cash, local fills seen)
        self._cash_strikes = 0

    def _expected_cash(self, base_cash: float, n_fills: int) -> float:
        """Broker cash implied by the fills applied locally since the baseline."""
        flow = 0.0
        for f in self.portfolio.fills[n_fills:]:
            flow -= f.signed_qty * f.price * self.portfolio.vpp(f.symbol) + f.fees
        return base_cash + flow

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

        cash_delta = 0.0
        broker_cash = broker_acct.get("cash")
        if broker_cash is not None:
            if self._cash_base is None:
                self._cash_base = (broker_cash, len(self.portfolio.fills))
            base_cash, n0 = self._cash_base
            cash_delta = broker_cash - self._expected_cash(base_cash, n0)
        cash_off = abs(cash_delta) > self.equity_tol * max(broker_acct.get("equity", 1.0), 1.0)
        # A fill can land at the broker between our two reads; require the mismatch to
        # persist across two checks before calling it drift.
        self._cash_strikes = self._cash_strikes + 1 if cash_off else 0
        cash_bad = self._cash_strikes >= 2

        ok = not divs and not cash_bad
        if not ok:
            self.halted = True
            if self._on_divergence:
                self._on_divergence(divs, cash_delta)
        else:
            self.halted = False
            if self._on_ok:
                self._on_ok()
        return ReconcileResult(self.clock.now(), ok, divs, eq_delta, cash_delta)
