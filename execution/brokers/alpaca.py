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
* ``reduce_only`` orders close a position. The open stop/target legs hold the
  shares, so they are cancelled first (otherwise the orphaned stop could later
  open a position the other way). If the close then fails, the protection is
  re-created so a position is never left naked.

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
        # protective legs cancelled for a close that has not gone through yet, so a
        # retry (or a failure) can still restore them: symbol -> [{type, price, qty}]
        self._saved_protection: dict[str, list[dict]] = {}

    # ------------------------------------------------------------- orders #
    async def submit(self, order: Order) -> str:
        if order.client_order_id in self._coid_to_broker:
            return self._coid_to_broker[order.client_order_id]  # idempotency
        if order.reduce_only:
            return await self._submit_close(order)
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

    # ----------------------------------------------------------- closing #
    async def _position(self, symbol: str) -> tuple[float, float]:
        """(signed qty, qty available to trade); (0, 0) when flat."""
        try:
            p = await self.http.request("GET", f"{self.base}/v2/positions/{symbol}")
        except AlpacaError as exc:
            if exc.status == 404:
                return 0.0, 0.0
            raise
        qty = abs(float(p["qty"]))
        sign = -1.0 if p.get("side") == "short" else 1.0
        return sign * qty, abs(float(p.get("qty_available", p["qty"])))

    async def _submit_close(self, order: Order) -> str:
        symbol = _check_symbol(order.symbol)
        pos, _ = await self._position(symbol)
        if pos == 0.0:
            raise RuntimeError(f"no_position: nothing to close for {symbol}")
        if (pos > 0) != (order.side is Side.SELL):
            # closing a long needs a SELL and closing a short a BUY; anything else
            # would open or add, which a reduce-only order must never do
            raise RuntimeError(f"no_position: {order.side.value} would not reduce {symbol} ({pos:g})")
        qty = min(order.qty, abs(pos))

        # 1. take the protective legs off (they hold the shares), remembering them
        open_orders = await self.http.request(
            "GET", f"{self.base}/v2/orders", params={"status": "open", "symbols": symbol,
                                                     "limit": 50}) or []
        legs = [
            {"type": o["type"], "qty": o["qty"],
             "price": o.get("stop_price") if o["type"] == "stop" else o.get("limit_price")}
            for o in open_orders  # every open order on this symbol is a protective leg
        ]
        if legs:
            self._saved_protection[symbol] = legs
        for o in open_orders:
            try:
                await self.http.request("DELETE", f"{self.base}/v2/orders/{o['id']}")
            except AlpacaError as exc:
                if exc.status not in (404, 422):  # already filled / gone
                    raise
        try:
            await self._wait_available(symbol, qty)
            resp = await self.http.request("POST", f"{self.base}/v2/orders", body={
                "symbol": symbol, "qty": f"{qty:g}", "side": order.side.value,
                "type": "market", "time_in_force": "day",
                "client_order_id": order.client_order_id,
            })
        except AlpacaError as exc:
            if exc.status == 422 and "client_order_id" in exc.body.lower():
                resp = await self._by_client_id(order.client_order_id)  # resubmit, already in
                if resp is None:
                    await self._restore_protection(symbol, order)
                    raise
            else:
                await self._restore_protection(symbol, order)
                raise
        except Exception:
            await self._restore_protection(symbol, order)
            raise
        broker_id = str(resp["id"])
        self._coid_to_broker[order.client_order_id] = broker_id
        self._order_to_coid[broker_id] = order.client_order_id
        self._saved_protection.pop(symbol, None)
        return broker_id

    async def _wait_available(self, symbol: str, qty: float, tries: int = 20) -> None:
        """Cancels are asynchronous: wait until the shares are free to trade."""
        for _ in range(tries):
            _, avail = await self._position(symbol)
            if avail + 1e-9 >= qty:
                return
            await asyncio.sleep(0.5)
        raise RuntimeError(f"timeout: protective orders for {symbol} still cancelling")

    async def _restore_protection(self, symbol: str, order: Order) -> None:
        """Best effort: put the stop/target back so the position is not left naked."""
        legs = self._saved_protection.get(symbol) or []
        exit_side = order.side.value  # the side that reduces the position
        stop = next((x for x in legs if x["type"] == "stop"), None)
        limit = next((x for x in legs if x["type"] == "limit"), None)
        if stop is None and limit is None:
            return
        qty = (stop or limit)["qty"]
        body: dict = {"symbol": symbol, "qty": str(qty), "side": exit_side, "time_in_force": "gtc"}
        if stop and limit:
            body |= {"order_class": "oco", "type": "limit",
                     "take_profit": {"limit_price": f"{float(limit['price']):.2f}"},
                     "stop_loss": {"stop_price": f"{float(stop['price']):.2f}"}}
        elif stop:
            body |= {"type": "stop", "stop_price": f"{float(stop['price']):.2f}"}
        else:
            body |= {"type": "limit", "limit_price": f"{float(limit['price']):.2f}"}
        try:
            await self.http.request("POST", f"{self.base}/v2/orders", body=body)
            self._saved_protection.pop(symbol, None)
        except Exception as exc:  # noqa: BLE001 - nothing more we can do here; surface it
            raise RuntimeError(
                f"CRITICAL: close of {symbol} failed AND protection could not be restored: {exc!r}"
            ) from exc

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
