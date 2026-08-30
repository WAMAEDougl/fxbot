"""Interactive Brokers order adapter (equities + spot FX) via ``ib_async``.

SCAFFOLD - build order step 6. Implements the BrokerAdapter protocol against
IB's native bracket orders (``ib.bracketOrder``), so stop-loss and take-profit
go to the venue *with* the entry - the design's hard rule.

API keys must be scoped to trading only - no withdrawal permission, ever.
Credentials come from the environment, never the repo, never the research plane.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC

from core.events import Fill, Order, OrderType, Side
from data.adapters.ibkr import _resolve_contract

try:  # pragma: no cover
    from ib_async import IB, LimitOrder, MarketOrder, StopOrder

    _HAVE_IB = True
except ImportError:  # pragma: no cover
    _HAVE_IB = False
    IB = LimitOrder = MarketOrder = StopOrder = object  # type: ignore


class IbkrBroker:
    broker = "ibkr"

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        client_id: int = 21,
        account: str | None = None,
    ) -> None:
        if not _HAVE_IB:
            raise ImportError('ib_async not installed. `pip install "trading-system[ibkr]"`')
        self.host = host or os.environ.get("IB_HOST", "127.0.0.1")
        self.port = int(port or os.environ.get("IB_PORT", "7497"))
        self.client_id = client_id
        self.account = account or os.environ.get("IB_ACCOUNT", "")
        self._ib: IB | None = None
        self._coid_to_broker: dict[str, str] = {}

    async def connect(self) -> None:
        if self._ib and self._ib.isConnected():
            return
        self._ib = IB()
        await self._ib.connectAsync(self.host, self.port, clientId=self.client_id)

    async def submit(self, order: Order) -> str:
        await self.connect()
        assert self._ib is not None
        if order.client_order_id in self._coid_to_broker:
            return self._coid_to_broker[order.client_order_id]  # idempotency

        contract = _resolve_contract(order.symbol)
        await self._ib.qualifyContractsAsync(contract)
        action = "BUY" if order.side is Side.BUY else "SELL"

        parent_limit = order.limit_price if order.order_type is OrderType.LIMIT else None
        bracket = self._ib.bracketOrder(
            action,
            order.qty,
            limitPrice=parent_limit if parent_limit is not None else _mkt_sentinel(),
            takeProfitPrice=order.take_profit,
            stopLossPrice=order.stop_loss,
        )
        if parent_limit is None:
            bracket.parent.orderType = "MKT"
        for o in bracket:
            o.orderRef = order.client_order_id
            if self.account:
                o.account = self.account
        trades = [self._ib.placeOrder(contract, o) for o in bracket]
        broker_id = str(trades[0].order.orderId)
        self._coid_to_broker[order.client_order_id] = broker_id
        return broker_id

    async def cancel(self, client_order_id: str) -> None:
        await self.connect()
        assert self._ib is not None
        for trade in self._ib.openTrades():
            if trade.order.orderRef == client_order_id:
                self._ib.cancelOrder(trade.order)

    async def positions(self) -> dict[str, float]:
        await self.connect()
        assert self._ib is not None
        out: dict[str, float] = {}
        for p in self._ib.positions(self.account or ""):
            out[_sym_of(p.contract)] = float(p.position)
        return out

    async def account(self) -> dict[str, float]:
        await self.connect()
        assert self._ib is not None
        vals = {v.tag: v.value for v in self._ib.accountValues(self.account or "")
                if v.currency in ("BASE", "USD", "")}
        def g(tag: str) -> float:
            try:
                return float(vals.get(tag, 0.0))
            except ValueError:
                return 0.0
        return {
            "equity": g("NetLiquidation"),
            "cash": g("TotalCashValue"),
            "margin_used": g("MaintMarginReq"),
            "margin_available": g("AvailableFunds"),
        }

    async def stream_fills(self) -> AsyncIterator[Fill]:  # pragma: no cover
        await self.connect()
        assert self._ib is not None
        import asyncio

        q: asyncio.Queue[Fill] = asyncio.Queue()

        def _on_exec(trade, fill):  # noqa: ANN001
            ex = fill.execution
            q.put_nowait(
                Fill(
                    ts=ex.time.astimezone(UTC),
                    order_id=trade.order.orderRef or str(ex.orderId),
                    symbol=_sym_of(trade.contract),
                    side=Side.BUY if ex.side == "BOT" else Side.SELL,
                    price=float(ex.price),
                    qty=float(ex.shares),
                    fees=float(getattr(fill.commissionReport, "commission", 0.0) or 0.0),
                    is_partial=False,
                )
            )

        self._ib.execDetailsEvent += _on_exec
        try:
            while True:
                yield await q.get()
        finally:
            self._ib.execDetailsEvent -= _on_exec


def _mkt_sentinel() -> float:
    return 0.0


def _sym_of(contract) -> str:  # noqa: ANN001
    if getattr(contract, "secType", "") == "CASH":
        return f"{contract.symbol}{contract.currency}"
    return contract.symbol
