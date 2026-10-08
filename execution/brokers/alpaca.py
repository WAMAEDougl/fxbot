"""Alpaca order adapter (US equities, paper or live) over the REST trading API.

Implements ``BrokerAdapter``. Entry, stop-loss and take-profit are sent as one
native bracket (``order_class=bracket``), so the exits live at the venue with
the entry - the design's hard rule.

* Bracket orders are always ``gtc``: a ``day`` bracket would lose its protective
  legs at the close, which is exactly wrong for a swing strategy.
* Bracket orders do not allow fractional quantities.
* ``client_order_id`` is the idempotency key. A resubmit returns the existing
  order instead of creating a second one.
* Alpaca gives bracket legs their own ids; fills of a leg are mapped back to the
  parent's ``client_order_id`` so the OMS sees exits under the entry's id, the
  same as the IBKR adapter's ``orderRef`` behaviour.
* Fills are polled from the account-activities endpoint (daily/swing horizon,
  no websocket needed). Fills that happen while the process is down are not
  replayed; the reconciler corrects positions against the broker on restart.
* Alpaca is commission-free, so ``Fill.fees`` is 0.

The endpoint is derived from ``env`` ("paper" | "live"). Alpaca also rejects
paper keys on the live host and vice versa, a second line of defence.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from core.events import Fill, Order, OrderType, Side
from data.adapters.alpaca import (
    LIVE_URL,
    PAPER_URL,
    AlpacaError,
    AlpacaHttp,
    _check_symbol,
    _iso,
)


class AlpacaBroker:
    broker = "alpaca"

    def __init__(self, *, env: str = "paper", http: AlpacaHttp | None = None,
                 poll_interval_s: float = 5.0) -> None:
        if env not in ("paper", "live"):
            raise ValueError(f"env must be 'paper' or 'live', got {env!r}")
        self.env = env
        self.base = PAPER_URL if env == "paper" else LIVE_URL
        self.http = http or AlpacaHttp()
        self.poll_interval_s = poll_interval_s
        self._coid_to_broker: dict[str, str] = {}
        self._order_to_coid: dict[str, str] = {}  # broker order id (entry or leg) -> our coid
        self._seen_fills: set[str] = set()

    # ------------------------------------------------------------- orders #
    async def submit(self, order: Order) -> str:
        if order.client_order_id in self._coid_to_broker:
            return self._coid_to_broker[order.client_order_id]  # idempotency
        symbol = _check_symbol(order.symbol)
        if order.qty != int(order.qty):
            raise ValueError(f"Alpaca bracket orders need whole shares, got qty={order.qty}")
        body: dict = {
            "symbol": symbol,
            "qty": str(int(order.qty)),
            "side": order.side.value,
            "type": "limit" if order.order_type is OrderType.LIMIT else "market",
            "time_in_force": "gtc",
            "order_class": "bracket",
            "client_order_id": order.client_order_id,
            "take_profit": {"limit_price": f"{order.take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{order.stop_loss:.2f}"},
        }
        if order.order_type is OrderType.LIMIT:
            body["limit_price"] = f"{order.limit_price:.2f}"
        try:
            resp = await self.http.request("POST", f"{self.base}/v2/orders", body=body)
        except AlpacaError as exc:
            if exc.status == 422 and "client_order_id" in exc.body.lower():
                resp = await self._by_client_id(order.client_order_id)
                if resp is None:
                    raise
            else:
                raise
        broker_id = str(resp["id"])
        self._coid_to_broker[order.client_order_id] = broker_id
        self._order_to_coid[broker_id] = order.client_order_id
        for leg in resp.get("legs") or []:
            self._order_to_coid[str(leg["id"])] = order.client_order_id
        return broker_id

    async def _by_client_id(self, coid: str) -> dict | None:
        try:
            return await self.http.request(
                "GET", f"{self.base}/v2/orders:by_client_order_id",
                params={"client_order_id": coid})
        except AlpacaError as exc:
            if exc.status == 404:
                return None
            raise

    async def cancel(self, client_order_id: str) -> None:
        order = await self._by_client_id(client_order_id)
        if order is None:
            return
        try:  # cancelling the parent cancels its un-triggered legs
            await self.http.request("DELETE", f"{self.base}/v2/orders/{order['id']}")
        except AlpacaError as exc:
            if exc.status not in (404, 422):  # already filled / already gone
                raise

    # ------------------------------------------------------ account state #
    async def positions(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in await self.http.request("GET", f"{self.base}/v2/positions") or []:
            qty = abs(float(p["qty"]))
            out[p["symbol"]] = -qty if p.get("side") == "short" else qty
        return out

    async def account(self) -> dict[str, float]:
        a = await self.http.request("GET", f"{self.base}/v2/account")

        def g(key: str) -> float:
            try:
                return float(a.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return {
            "equity": g("equity"),
            "cash": g("cash"),
            "margin_used": g("maintenance_margin"),
            "margin_available": g("buying_power"),
        }

    # -------------------------------------------------------------- fills #
    async def _coid_for(self, order_id: str) -> str | None:
        if order_id in self._order_to_coid:
            return self._order_to_coid[order_id]
        # Unknown id (typically a bracket leg after a restart): rebuild the map
        # from recent orders with their nested legs.
        recent = await self.http.request(
            "GET", f"{self.base}/v2/orders",
            params={"status": "all", "nested": "true", "limit": 500,
                    "after": _iso(datetime.now(UTC) - timedelta(days=14))})
        for parent in recent or []:
            coid = parent.get("client_order_id")
            self._order_to_coid[str(parent["id"])] = coid
            for leg in parent.get("legs") or []:
                self._order_to_coid[str(leg["id"])] = coid
        return self._order_to_coid.get(order_id)

    async def poll_fills(self, after: datetime) -> list[Fill]:
        acts = await self.http.request(
            "GET", f"{self.base}/v2/account/activities/FILL",
            params={"after": _iso(after), "direction": "asc", "page_size": 100}) or []
        fills: list[Fill] = []
        for a in acts:
            if a["id"] in self._seen_fills:
                continue
            self._seen_fills.add(a["id"])
            coid = await self._coid_for(str(a["order_id"]))
            if coid is None:
                continue  # not ours (placed outside this system)
            ts = datetime.fromisoformat(a["transaction_time"].replace("Z", "+00:00")).astimezone(UTC)
            fills.append(Fill(
                ts=ts, order_id=coid, symbol=a["symbol"],
                side=Side.BUY if a["side"] == "buy" else Side.SELL,
                price=float(a["price"]), qty=float(a["qty"]), fees=0.0,
                is_partial=a.get("type") == "partial_fill",
            ))
        return fills

    async def stream_fills(self) -> AsyncIterator[Fill]:  # pragma: no cover - network loop
        after = datetime.now(UTC) - timedelta(minutes=1)
        while True:
            for fill in await self.poll_fills(after):
                after = max(after, fill.ts)
                yield fill
            await asyncio.sleep(self.poll_interval_s)
