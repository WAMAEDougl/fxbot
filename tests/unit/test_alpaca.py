import json
from datetime import UTC, datetime, timedelta

import pytest

from core.events import Order, OrderType, Side
from data.adapters.alpaca import AlpacaError, AlpacaFeed, AlpacaHttp
from execution.brokers.alpaca import AlpacaBroker

NOW = datetime(2024, 3, 20, 12, tzinfo=UTC)


class FakeAlpaca:
    """Routes (method, path) -> (status, json). Records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body):
        path = url.split("?")[0].split(".alpaca.markets")[1]
        self.calls.append((method, path, url, json.loads(body) if body else None, headers))
        res = self.routes[(method, path)]
        res = res.pop(0) if isinstance(res, list) else res
        status, payload = res
        return status, json.dumps(payload)


def _http(routes):
    t = FakeAlpaca(routes)
    return AlpacaHttp(transport=t), t


def _order(**kw):
    base = dict(ts=NOW, client_order_id="c1", symbol="AAPL", side=Side.BUY, qty=10,
                order_type=OrderType.MARKET, limit_price=None, stop_loss=95.0,
                take_profit=110.0, strategy_id="s")
    base.update(kw)
    return Order(**base)


async def test_submit_sends_gtc_bracket_and_is_idempotent():
    http, t = _http({("POST", "/v2/orders"): (200, {"id": "A1", "legs": [{"id": "L1"}, {"id": "L2"}]})})
    b = AlpacaBroker(http=http)
    assert await b.submit(_order()) == "A1"
    assert await b.submit(_order()) == "A1"  # no second POST
    posts = [c for c in t.calls if c[0] == "POST"]
    assert len(posts) == 1
    body = posts[0][3]
    assert body["order_class"] == "bracket" and body["time_in_force"] == "gtc"
    assert body["stop_loss"] == {"stop_price": "95.00"}
    assert body["take_profit"] == {"limit_price": "110.00"}
    assert body["client_order_id"] == "c1" and body["qty"] == "10"
    assert posts[0][2].startswith("https://paper-api.alpaca.markets")


async def test_live_env_uses_live_host():
    http, t = _http({("GET", "/v2/account"): (200, {"equity": "5"})})
    await AlpacaBroker(env="live", http=http).account()
    assert t.calls[0][2].startswith("https://api.alpaca.markets")


async def test_duplicate_client_id_recovers_existing_order():
    http, _ = _http({
        ("POST", "/v2/orders"): (422, {"message": "client_order_id must be unique"}),
        ("GET", "/v2/orders:by_client_order_id"): (200, {"id": "A9"}),
    })
    assert await AlpacaBroker(http=http).submit(_order()) == "A9"


async def test_fractional_qty_rejected():
    http, _ = _http({})
    with pytest.raises(ValueError):
        await AlpacaBroker(http=http).submit(_order(qty=1.5))


async def test_fx_symbol_rejected():
    http, _ = _http({})
    with pytest.raises(ValueError):
        await AlpacaBroker(http=http).submit(_order(symbol="EUR.USD"))


async def test_error_message_is_classifiable_by_oms():
    http, _ = _http({("POST", "/v2/orders"): (429, {"message": "slow down"})})
    with pytest.raises(AlpacaError, match="rate_limit"):
        await AlpacaBroker(http=http).submit(_order())


async def test_positions_and_account():
    http, _ = _http({
        ("GET", "/v2/positions"): (200, [
            {"symbol": "AAPL", "qty": "10", "side": "long"},
            {"symbol": "MSFT", "qty": "-4", "side": "short"},
        ]),
        ("GET", "/v2/account"): (200, {"equity": "100000.5", "cash": "90000",
                                       "maintenance_margin": "100", "buying_power": "200000"}),
    })
    b = AlpacaBroker(http=http)
    assert await b.positions() == {"AAPL": 10.0, "MSFT": -4.0}
    acct = await b.account()
    assert acct["equity"] == 100000.5 and acct["margin_available"] == 200000.0


async def test_leg_fill_maps_to_parent_client_order_id_and_dedupes():
    act = lambda i, oid, side, typ: {  # noqa: E731
        "id": i, "order_id": oid, "symbol": "AAPL", "side": side, "qty": "10",
        "price": "101.5", "transaction_time": "2024-03-20T14:00:00Z", "type": typ}
    http, _ = _http({
        ("POST", "/v2/orders"): (200, {"id": "A1", "legs": [{"id": "L1"}, {"id": "L2"}]}),
        ("GET", "/v2/account/activities/FILL"): [
            (200, [act("f1", "A1", "buy", "fill"), act("f2", "L1", "sell", "fill")]),
            (200, [act("f1", "A1", "buy", "fill")]),
        ],
    })
    b = AlpacaBroker(http=http)
    await b.submit(_order())
    fills = await b.poll_fills(NOW - timedelta(days=1))
    assert [(f.order_id, f.side) for f in fills] == [("c1", Side.BUY), ("c1", Side.SELL)]
    assert await b.poll_fills(NOW - timedelta(days=1)) == []  # already seen


async def test_unknown_leg_resolved_from_nested_orders():
    http, _ = _http({
        ("GET", "/v2/account/activities/FILL"): (200, [{
            "id": "f9", "order_id": "L7", "symbol": "AAPL", "side": "sell", "qty": "3",
            "price": "99", "transaction_time": "2024-03-20T14:00:00Z", "type": "partial_fill"}]),
        ("GET", "/v2/orders"): (200, [{"id": "P7", "client_order_id": "cX",
                                       "legs": [{"id": "L7"}]}]),
    })
    fills = await AlpacaBroker(http=http).poll_fills(NOW - timedelta(days=1))
    assert fills[0].order_id == "cX" and fills[0].is_partial


async def test_feed_backfill_pages_and_filters_range():
    bar = lambda t, c: {"t": t, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1000}  # noqa: E731
    http, t = _http({("GET", "/v2/stocks/AAPL/bars"): [
        (200, {"bars": [bar("2024-03-18T04:00:00Z", 100)], "next_page_token": "tok"}),
        (200, {"bars": [bar("2024-03-19T04:00:00Z", 101), bar("2023-01-01T05:00:00Z", 1)],
               "next_page_token": None}),
    ]})
    bars = await AlpacaFeed(http).backfill("AAPL", NOW - timedelta(days=10), NOW, "1d")
    assert [b.close for b in bars] == [100, 101]
    assert bars[0].source == "alpaca" and bars[0].bar_interval == "1d"
    assert "page_token=tok" in t.calls[1][2]
    assert "adjustment=split" in t.calls[0][2]


async def test_feed_rejects_fx():
    http, _ = _http({})
    with pytest.raises(ValueError):
        await AlpacaFeed(http).backfill("EUR.USD", NOW - timedelta(days=1), NOW)


def test_events_construct_under_slots_dataclass():
    """Regression: zero-arg super() in a slots=True dataclass raised TypeError on 3.12."""
    from core.events import Direction, Signal

    Signal(ts=NOW, symbol="AAPL", direction=Direction.LONG, strategy_id="s",
           entry_hint=100.0, stop_hint=95.0, target_hint=110.0)
    _order()
