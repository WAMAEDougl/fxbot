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
* ``reduce_only`` orders close a position WITHOUT ever removing its protection first. The
  position is sold through its own take-profit leg: that leg's price is moved to a marketable
  level, so the stop stays live until the shares are actually sold, and the OCO link cancels the
  stop automatically on the fill. (An earlier design cancelled the legs first; after hours Alpaca
  defers the cancel of a live leg until the next session, which blocked the sell and left a
  position naked for 26 hours.) A position with no protective orders gets a plain market sell;
  one with only a stop is refused loudly and left untouched.

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

# order states that are finished (or already being cancelled): not protection any more
_FINISHED = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced",
             "pending_cancel", "suspended", "stopped"}


# states in which Alpaca lets a working order be amended
_REPLACEABLE = {"new", "partially_filled"}

# how far inside the market the replaced take-profit is priced so it sells at once
_CLOSE_BUFFER = 0.03


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
        self.verify_retries, self.verify_wait_s = 5, 1.0  # re-read legs after a replace

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
    async def _position(self, symbol: str) -> dict | None:
        """Signed qty, qty free to trade and last price; None when flat."""
        try:
            p = await self.http.request("GET", f"{self.base}/v2/positions/{symbol}")
        except AlpacaError as exc:
            if exc.status == 404:
                return None
            raise
        qty = abs(float(p["qty"]))
        return {"qty": -qty if p.get("side") == "short" else qty,
                "available": abs(float(p.get("qty_available", p["qty"]))),
                "price": float(p.get("current_price") or 0.0)}

    async def _submit_close(self, order: Order) -> str:
        symbol = _check_symbol(order.symbol)
        pos = await self._position(symbol)
        if pos is None or pos["qty"] == 0.0:
            raise RuntimeError(f"no_position: nothing to close for {symbol}")
        if (pos["qty"] > 0) != (order.side is Side.SELL):
            # closing a long needs a SELL and closing a short a BUY; anything else
            # would open or add, which a reduce-only order must never do
            raise RuntimeError(
                f"no_position: {order.side.value} would not reduce {symbol} ({pos['qty']:g})")
        have = abs(pos["qty"])
        if order.qty + 1e-9 < have:
            raise RuntimeError(
                f"unsupported: partial close ({order.qty:g} of {have:g}) is not supported")

        legs = await self._protective_orders(symbol, order.side)
        take = next((o for o in legs if o["type"] == "limit"), None)
        stop = next((o for o in legs if o["type"] in ("stop", "stop_limit")), None)

        if take is None and stop is None:  # nothing protects it and nothing holds the shares
            return await self._market_close(order, symbol, have)
        if take is None:
            raise RuntimeError(
                f"unsupported: only a stop is live for {symbol}; not touching it. Close manually")

        if take.get("status") not in _REPLACEABLE:
            # e.g. "accepted": an order placed after hours cannot be amended until the session
            # opens. Not a failure: wait (the OMS retries closes patiently); nothing is touched.
            raise RuntimeError(
                f"timeout: the take-profit order is '{take.get('status')}', not amendable yet")

        # Sell through the take-profit leg. Nothing is cancelled, so if this call fails the
        # stop and target are exactly as they were.
        stop_px = float(stop["stop_price"]) if stop and stop.get("stop_price") else None
        limit = self._marketable_limit(order.side, pos["price"], stop_px)
        try:
            resp = await self.http.request(
                "PATCH", f"{self.base}/v2/orders/{take['id']}",
                body={"limit_price": f"{limit:.2f}"})
        except AlpacaError as exc:
            if exc.status == 422 and "cannot replace" in exc.body.lower():
                raise RuntimeError(f"timeout: Alpaca cannot amend the order yet: {exc.body[:120]}") from exc
            raise
        broker_id = str(resp["id"])  # a replace returns a NEW order id
        self._coid_to_broker[order.client_order_id] = broker_id
        for oid in (broker_id, str(take["id"])):
            self._order_to_coid[oid] = order.client_order_id  # its fill is THIS close's fill
        if stop is not None:
            await self._ensure_stop(symbol, order.side, have, stop_px)
        return broker_id

    @staticmethod
    def _marketable_limit(exit_side: Side, price: float, stop_px: float | None) -> float:
        """A limit that sells (or buys to cover) at the market, kept on the right side of the
        stop (a take-profit may not cross its own stop)."""
        if price <= 0:
            raise RuntimeError("timeout: no current price for the position yet")
        if exit_side is Side.SELL:
            lim = round(price * (1.0 - _CLOSE_BUFFER), 2)
            return max(lim, round(stop_px + 0.01, 2)) if stop_px else lim
        lim = round(price * (1.0 + _CLOSE_BUFFER), 2)
        return min(lim, round(stop_px - 0.01, 2)) if stop_px else lim

    async def _market_close(self, order: Order, symbol: str, qty: float) -> str:
        try:
            resp = await self.http.request("POST", f"{self.base}/v2/orders", body={
                "symbol": symbol, "qty": f"{qty:g}", "side": order.side.value, "type": "market",
                "time_in_force": "day", "client_order_id": order.client_order_id})
        except AlpacaError as exc:
            if exc.status == 422 and "client_order_id" in exc.body.lower():
                resp = await self._by_client_id(order.client_order_id)  # already submitted
                if resp is None:
                    raise
            else:
                raise
        broker_id = str(resp["id"])
        self._coid_to_broker[order.client_order_id] = broker_id
        self._order_to_coid[broker_id] = order.client_order_id
        return broker_id

    async def _ensure_stop(
        self, symbol: str, exit_side: Side, qty: float, stop_px: float | None
    ) -> None:
        """Safety net after a replace: the stop must still be live. Re-read a few times (the
        listing can lag); only if it is really gone, put a plain stop back."""
        for _ in range(self.verify_retries):
            live = await self._protective_orders(symbol, exit_side)
            if any(o["type"] in ("stop", "stop_limit") for o in live):
                return
            await asyncio.sleep(self.verify_wait_s)
        if stop_px is None:
            raise RuntimeError(f"CRITICAL: the stop on {symbol} vanished after the replace")
        try:
            await self.http.request("POST", f"{self.base}/v2/orders", body={
                "symbol": symbol, "qty": f"{qty:g}", "side": exit_side.value, "type": "stop",
                "stop_price": f"{stop_px:.2f}", "time_in_force": "gtc"})
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"CRITICAL: the stop on {symbol} vanished after the replace and could not be "
                f"re-placed: {exc!r}") from exc

    async def _protective_orders(self, symbol: str, exit_side: Side) -> list[dict]:
        """Every not-yet-finished stop/limit order on ``symbol`` that would REDUCE the position
        (side == ``exit_side``), including bracket/OCO legs in state "held"."""
        listed = await self.http.request(
            "GET", f"{self.base}/v2/orders",
            params={"status": "all", "symbols": symbol, "nested": "true", "limit": 100,
                    "direction": "desc",
                    "after": _iso(datetime.now(UTC) - timedelta(days=60))}) or []
        found: dict[str, dict] = {}

        def consider(o: dict) -> None:
            if (o.get("status") not in _FINISHED and o.get("side") == exit_side.value
                    and o.get("type") in ("stop", "limit", "stop_limit")):
                found[o["id"]] = o

        for o in listed:
            consider(o)
            for leg in o.get("legs") or []:
                consider(leg)
        return list(found.values())

    async def live_exit_orders(self) -> dict[str, list[dict]]:
        """Live stop/limit orders that protect a position that EXISTS (its entry has filled) or
        stand alone (an OCO, a plain stop), grouped by symbol. The legs of an entry that has
        not filled yet are left out: they protect nothing and are not orphans."""
        listed = await self.http.request(
            "GET", f"{self.base}/v2/orders",
            params={"status": "all", "nested": "true", "limit": 500, "direction": "desc",
                    "after": _iso(datetime.now(UTC) - timedelta(days=60))}) or []
        out: dict[str, list[dict]] = {}
        for o in listed:
            cls = o.get("order_class")
            if cls == "bracket":
                cands = list(o.get("legs") or []) if o.get("status") == "filled" else []
            elif cls == "oco":
                cands = [o, *(o.get("legs") or [])]
            elif o.get("type") in ("stop", "stop_limit"):
                cands = [o]  # a plain stop (e.g. a safety stop)
            else:
                cands = []  # market/limit entries and their legs while unfilled
            for c in cands:
                if c.get("status") not in _FINISHED and c.get("type") in (
                        "stop", "limit", "stop_limit"):
                    out.setdefault(c["symbol"], []).append(c)
        return out

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
