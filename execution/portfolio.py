"""Portfolio / local state store (design 4.6).

Single source of *local* truth: positions, equity curve, open orders, realised
and unrealised P/L, per-strategy attribution. The broker remains the source of
*authoritative* truth (see execution/reconcile.py).

This same class backs both the backtester and the live engine - it only consumes
``MarketData`` and ``Fill`` events, it never talks to anything. Persistence
(SQLite/DuckDB write-ahead) is layered on in ``persist.py``; the in-memory core
is here and is deterministic.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime

from core.events import Fill, MarketData


@dataclass(slots=True)
class Position:
    symbol: str
    qty: float = 0.0  # signed
    avg_price: float = 0.0
    realised_pnl: float = 0.0
    fees_paid: float = 0.0
    last_price: float = 0.0
    opened_ts: datetime | None = None
    origin_signal_id: str = ""
    origin_features_hash: str = ""

    @property
    def unrealised_pnl(self) -> float:
        if self.qty == 0.0:
            return 0.0
        return (self.last_price - self.avg_price) * self.qty


class Portfolio:
    def __init__(
        self,
        starting_equity: float,
        *,
        base_ccy: str = "USD",
        history_len: int = 512,
        value_per_point: dict[str, float] | None = None,
    ) -> None:
        self.starting_equity = starting_equity
        self.base_ccy = base_ccy
        self._cash = starting_equity
        self._positions: dict[str, Position] = {}
        self._history: dict[str, deque[MarketData]] = {}
        self._history_len = history_len
        self._now: datetime | None = None
        self.value_per_point = value_per_point or {}
        # attribution
        self.realised_by_strategy: dict[str, float] = {}
        self.equity_curve: list[tuple[datetime, float]] = []
        self.fills: list[Fill] = []

    # -- ReadOnlyContext surface --------------------------------------- #
    @property
    def equity(self) -> float:
        return self._cash + sum(p.unrealised_pnl for p in self._positions.values())

    @property
    def now(self) -> datetime:
        if self._now is None:
            raise RuntimeError("portfolio has not seen any market data yet")
        return self._now

    @property
    def cash(self) -> float:
        return self._cash

    def position(self, symbol: str) -> float:
        p = self._positions.get(symbol)
        return p.qty if p else 0.0

    def avg_price(self, symbol: str) -> float | None:
        p = self._positions.get(symbol)
        return p.avg_price if p and p.qty != 0.0 else None

    def unrealised_pnl(self, symbol: str) -> float:
        p = self._positions.get(symbol)
        return p.unrealised_pnl if p else 0.0

    def history(self, symbol: str, lookback: int) -> list[MarketData]:
        buf = self._history.get(symbol)
        if not buf:
            return []
        return list(buf)[-lookback:]

    def open_symbols(self) -> list[str]:
        return [s for s, p in self._positions.items() if p.qty != 0.0]

    def vpp(self, symbol: str) -> float:
        return self.value_per_point.get(symbol, 1.0)

    # -- event handlers ---------------------------------------------- #
    def on_market_data(self, ev: MarketData) -> None:
        self._now = ev.ts
        buf = self._history.setdefault(symbol := ev.symbol, deque(maxlen=self._history_len))
        buf.append(ev)
        p = self._positions.get(symbol)
        if p is not None:
            p.last_price = ev.price
        self.equity_curve.append((ev.ts, self.equity))

    def on_fill(self, fill: Fill) -> None:
        self.fills.append(fill)
        self._now = fill.ts
        p = self._positions.setdefault(fill.symbol, Position(symbol=fill.symbol))
        signed = fill.signed_qty
        vpp = self.vpp(fill.symbol)

        self._cash -= fill.fees
        p.fees_paid += fill.fees

        if p.qty == 0.0 or (p.qty > 0) == (signed > 0):
            # opening or adding in the same direction -> weighted avg
            new_qty = p.qty + signed
            if new_qty != 0.0:
                p.avg_price = (p.avg_price * abs(p.qty) + fill.price * abs(signed)) / abs(new_qty)
            p.qty = new_qty
            if p.opened_ts is None:
                p.opened_ts = fill.ts
                p.origin_signal_id = getattr(fill, "signal_id", "") or p.origin_signal_id
        else:
            # reducing / closing / flipping
            closing = min(abs(signed), abs(p.qty))
            direction = 1.0 if p.qty > 0 else -1.0
            pnl = (fill.price - p.avg_price) * direction * closing * vpp
            p.realised_pnl += pnl
            self._cash += pnl
            self.realised_by_strategy[fill.strategy_id] = (
                self.realised_by_strategy.get(fill.strategy_id, 0.0) + pnl
            )
            remaining = abs(signed) - closing
            p.qty += signed
            if abs(p.qty) < 1e-12:
                p.qty = 0.0
                p.avg_price = 0.0
                p.opened_ts = None
            if remaining > 1e-12:  # flipped through zero
                p.qty = remaining * (1.0 if signed > 0 else -1.0)
                p.avg_price = fill.price
                p.opened_ts = fill.ts

        p.last_price = fill.price

    # -- snapshots -------------------------------------------------- #
    def snapshot(self) -> dict:
        return {
            "ts": self._now,
            "equity": self.equity,
            "cash": self._cash,
            "positions": {
                s: {"qty": p.qty, "avg_price": p.avg_price, "upnl": p.unrealised_pnl}
                for s, p in self._positions.items()
                if p.qty != 0.0
            },
            "realised_by_strategy": dict(self.realised_by_strategy),
        }
