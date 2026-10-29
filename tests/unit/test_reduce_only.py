"""Reduce-only (close-on-signal) orders: the one bracket-free order kind.

The rule being protected: nothing that can ADD risk reaches a broker without a
stop-loss and a take-profit. A close may skip the bracket only because it cannot
open or flip a position.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from core.events import Direction, Order, OrderType, Side, Signal
from data.adapters.alpaca import AlpacaError, AlpacaHttp
from execution.brokers.alpaca import AlpacaBroker
from risk.manager import MarketState, RiskConfig, RiskManager
from risk.sizing import SizingParams

NOW = datetime(2024, 3, 20, 12, tzinfo=UTC)


def _close(**kw):
    base = dict(ts=NOW, client_order_id="c", symbol="AAPL", side=Side.SELL, qty=10,
                order_type=OrderType.MARKET, strategy_id="s", reduce_only=True)
    base.update(kw)
    return Order(**base)


# ------------------------------------------------------------- Order rules #
def test_reduce_only_needs_no_bracket():
    o = _close()
    assert o.stop_loss is None and o.take_profit is None


def test_reduce_only_cannot_carry_a_bracket_or_be_a_limit():
    with pytest.raises(ValueError):
        _close(stop_loss=95.0, take_profit=110.0)
    with pytest.raises(ValueError):
        _close(order_type=OrderType.LIMIT, limit_price=100.0)


def test_normal_orders_still_require_both_legs():
    base = dict(ts=NOW, client_order_id="c", symbol="AAPL", side=Side.BUY, qty=1,
                order_type=OrderType.MARKET, strategy_id="s")
    with pytest.raises(ValueError):
        Order(**base)
    with pytest.raises(ValueError):
        Order(**base, stop_loss=95.0)
    with pytest.raises(ValueError):
        Order(**base, take_profit=110.0)


# --------------------------------------------------------- risk manager #
class _Ctx:
    equity = 100_000.0

    def __init__(self, pos=0.0, now=NOW):
        self._pos, self.now = pos, now

    def position(self, s):
        return self._pos

    def avg_price(self, s):
        return None

    def unrealised_pnl(self, s):
        return 0.0

    def history(self, s, n):
        return []


def _market():
    return MarketState(last_bar_ts=NOW, bar_interval=timedelta(days=1), spread=0.02, atr=2.0,
                       price=100.0, value_per_point=1.0,
                       sizing=SizingParams(value_per_point=1.0, lot_step=1.0, min_lot=1.0),
                       margin_available=1e12, margin_rate=0.0)


def _flat(ts=NOW):
    return Signal(ts=ts, symbol="AAPL", direction=Direction.FLAT, strategy_id="s")


def test_flat_closes_the_whole_long_position_with_a_sell():
    d = RiskManager(RiskConfig()).evaluate(_flat(), _Ctx(pos=37.0), _market())
    assert d.order.reduce_only and d.order.side is Side.SELL and d.order.qty == 37.0


def test_flat_closes_a_short_with_a_buy():
    d = RiskManager(RiskConfig()).evaluate(_flat(), _Ctx(pos=-12.0), _market())
    assert d.order.reduce_only and d.order.side is Side.BUY and d.order.qty == 12.0


def test_flat_with_no_position_is_a_noop():
    d = RiskManager(RiskConfig()).evaluate(_flat(), _Ctx(pos=0.0), _market())
    assert d.order is None and d.rejection.reason_code == "NO_OP"


def test_close_still_works_when_halted_and_over_the_trade_cap():
    """Closing only reduces risk, so a halted system must still be able to get out."""
    rm = RiskManager(RiskConfig(max_trades_per_day=0), kill_switch=lambda: True)
    d = rm.evaluate(_flat(), _Ctx(pos=5.0), _market())
    assert d.order is not None and d.order.reduce_only


def test_duplicate_close_is_blocked_then_allowed_after_ttl_or_position_change():
    rm = RiskManager(RiskConfig())
    assert rm.evaluate(_flat(), _Ctx(pos=5.0), _market()).order is not None
    dup = rm.evaluate(_flat(NOW + timedelta(days=1)), _Ctx(5.0, NOW + timedelta(days=1)), _market())
    assert dup.order is None and dup.rejection.reason_code == "DUPLICATE_CLOSE"
    later = NOW + timedelta(days=4)
    assert rm.evaluate(_flat(later), _Ctx(5.0, later), _market()).order is not None
    other = rm.evaluate(_flat(later), _Ctx(7.0, later), _market())  # position changed
    assert other.order is not None


def test_entry_signals_still_always_carry_a_bracket():
    sig = Signal(ts=NOW, symbol="AAPL", direction=Direction.LONG, strategy_id="s",
                 entry_hint=100.0, stop_hint=94.0, target_hint=118.0)
    d = RiskManager(RiskConfig(min_stop_spread_mult=0.0)).evaluate(sig, _Ctx(), _market())
    assert d.order is not None and not d.order.reduce_only
    assert d.order.stop_loss is not None and d.order.take_profit is not None


# ------------------------------------------------------------ Alpaca close #
class _Fake:
    """Routes (method, path) -> (status, json) or a list consumed in order. Records calls."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body):
        path = url.split("?")[0].split(".alpaca.markets")[1]
        self.calls.append((method, path, json.loads(body) if body else None))
        res = self.routes[(method, path)]
        status, payload = res.pop(0) if isinstance(res, list) else res
        return status, json.dumps(payload)


def _broker(routes):
    fake = _Fake(routes)
    b = AlpacaBroker(http=AlpacaHttp(transport=fake))
    b.verify_wait_s = 0.0  # no real sleeping in tests
    return b, fake


def _kinds(f, *methods):
    return [(m, p) for m, p, _ in f.calls if m in methods]


# Structure captured from a REAL Alpaca paper position (EWJ): the parent is filled, the
# take-profit leg is "new", the stop leg is "held" and is invisible under status=open.
_REAL = [{
    "id": "P1", "type": "market", "side": "buy", "qty": "144", "status": "filled",
    "legs": [
        {"id": "T1", "type": "limit", "side": "sell", "qty": "144", "status": "new",
         "limit_price": "128.52", "stop_price": None},
        {"id": "S1", "type": "stop", "side": "sell", "qty": "144", "status": "held",
         "stop_price": "90.66", "limit_price": None},
    ],
}]


def _long_pos(price="94.13", avail="0"):
    return (200, {"qty": "144", "side": "long", "qty_available": avail, "current_price": price})


def _close_routes(over=None):
    r = {
        ("GET", "/v2/positions/EWJ"): _long_pos(),
        ("GET", "/v2/orders"): (200, _REAL),
        ("PATCH", "/v2/orders/T1"): (200, {"id": "T2", "status": "new"}),
    }
    r.update(over or {})
    return r


async def test_close_sells_through_the_take_profit_leg_and_never_cancels_protection():
    """The 28 Oct failure: cancelling legs after hours is deferred, so the sell could not be
    placed and the position ended up naked. The close must not cancel anything."""
    b, f = _broker(_close_routes())
    assert await b.submit(_close(symbol="EWJ", qty=144)) == "T2"
    assert _kinds(f, "DELETE", "POST") == []  # no cancel, no new order
    patch = [c for c in f.calls if c[0] == "PATCH"]
    assert [c[1] for c in patch] == ["/v2/orders/T1"]
    assert patch[0][2] == {"limit_price": "91.31"}  # 94.13 less 3%, still above the 90.66 stop


async def test_close_fill_is_attributed_to_the_close_order_not_the_entry():
    b, _ = _broker(_close_routes())
    await b.submit(_close(symbol="EWJ", qty=144, client_order_id="close-1"))
    assert b._order_to_coid["T2"] == "close-1" and b._order_to_coid["T1"] == "close-1"
    assert await b.submit(_close(symbol="EWJ", qty=144, client_order_id="close-1")) == "T2"  # idempotent


async def test_limit_is_kept_above_the_stop_when_the_price_is_close_to_it():
    b, f = _broker(_close_routes({("GET", "/v2/positions/EWJ"): _long_pos(price="91.00")}))
    await b.submit(_close(symbol="EWJ", qty=144))
    assert [c[2] for c in f.calls if c[0] == "PATCH"][0] == {"limit_price": "90.67"}  # stop + 0.01


async def test_short_position_is_covered_through_its_target_with_the_limit_below_the_stop():
    short = [{"id": "P1", "type": "market", "side": "sell", "qty": "50", "status": "filled", "legs": [
        {"id": "T1", "type": "limit", "side": "buy", "qty": "50", "status": "new",
         "limit_price": "80.00"},
        {"id": "S1", "type": "stop", "side": "buy", "qty": "50", "status": "held",
         "stop_price": "104.00"}]}]
    b, f = _broker({
        ("GET", "/v2/positions/XYZ"): (200, {"qty": "50", "side": "short", "qty_available": "0",
                                              "current_price": "100.00"}),
        ("GET", "/v2/orders"): (200, short),
        ("PATCH", "/v2/orders/T1"): (200, {"id": "T2"}),
    })
    await b.submit(_close(symbol="XYZ", side=Side.BUY, qty=50))
    assert [c[2] for c in f.calls if c[0] == "PATCH"][0] == {"limit_price": "103.00"}  # +3% < stop
    assert _kinds(f, "DELETE", "POST") == []


async def test_a_failed_replace_leaves_the_stop_and_target_exactly_as_they_were():
    b, f = _broker(_close_routes({("PATCH", "/v2/orders/T1"): (503, {"message": "down"})}))
    with pytest.raises(AlpacaError, match="5xx"):
        await b.submit(_close(symbol="EWJ", qty=144))
    assert _kinds(f, "DELETE", "POST") == []  # nothing cancelled, so nothing to restore


async def test_naked_position_gets_a_plain_market_sell():
    b, f = _broker(_close_routes({
        ("GET", "/v2/positions/EWJ"): _long_pos(avail="144"),
        ("GET", "/v2/orders"): (200, []),
        ("POST", "/v2/orders"): (200, {"id": "M1"}),
    }))
    assert await b.submit(_close(symbol="EWJ", qty=144)) == "M1"
    body = [c[2] for c in f.calls if c[0] == "POST"][0]
    assert body["type"] == "market" and body["side"] == "sell" and body["qty"] == "144"
    assert "order_class" not in body and "stop_loss" not in body
    assert _kinds(f, "DELETE", "PATCH") == []


async def test_only_a_stop_is_live_is_refused_and_left_untouched():
    only_stop = [{"id": "S1", "type": "stop", "side": "sell", "qty": "144", "status": "new",
                  "stop_price": "90.66"}]
    b, f = _broker(_close_routes({("GET", "/v2/orders"): (200, only_stop)}))
    with pytest.raises(RuntimeError, match="unsupported"):
        await b.submit(_close(symbol="EWJ", qty=144))
    assert _kinds(f, "DELETE", "POST", "PATCH") == []


async def test_finished_legs_and_entry_side_orders_are_ignored():
    mixed = [
        {"id": "P0", "type": "market", "side": "buy", "qty": "50", "status": "filled", "legs": [
            {"id": "OLD1", "type": "limit", "side": "sell", "qty": "50", "status": "canceled",
             "limit_price": "120.00"},
            {"id": "OLD2", "type": "stop", "side": "sell", "qty": "50", "status": "filled",
             "stop_price": "80.00"}]},
        {"id": "PEND", "type": "limit", "side": "buy", "qty": "5", "status": "new",
         "limit_price": "70.00", "legs": []},
    ] + _REAL
    b, f = _broker(_close_routes({("GET", "/v2/orders"): (200, mixed)}))
    await b.submit(_close(symbol="EWJ", qty=144))
    assert [c[1] for c in f.calls if c[0] == "PATCH"] == ["/v2/orders/T1"]  # the live target only


async def test_an_oco_order_placed_earlier_is_found_and_used():
    """An OCO re-placed after an incident: the take-profit is the top-level order and the
    stop sits in its legs."""
    oco = [{"id": "O1", "type": "limit", "side": "sell", "qty": "144", "status": "new",
            "limit_price": "128.52", "order_class": "oco",
            "legs": [{"id": "O2", "type": "stop", "side": "sell", "qty": "144", "status": "held",
                      "stop_price": "90.66"}]}]
    b, f = _broker(_close_routes({("GET", "/v2/orders"): (200, oco),
                                    ("PATCH", "/v2/orders/O1"): (200, {"id": "O3"})}))
    assert await b.submit(_close(symbol="EWJ", qty=144)) == "O3"
    assert _kinds(f, "DELETE", "POST") == []


async def test_if_the_stop_really_vanishes_after_the_replace_a_stop_is_placed_back():
    no_stop = [{"id": "T2", "type": "limit", "side": "sell", "qty": "144", "status": "new",
                "limit_price": "91.31"}]
    b, f = _broker(_close_routes({
        ("GET", "/v2/orders"): [(200, _REAL)] + [(200, no_stop)] * 5,
        ("POST", "/v2/orders"): (200, {"id": "S9"}),
    }))
    assert await b.submit(_close(symbol="EWJ", qty=144)) == "T2"
    stop = [c[2] for c in f.calls if c[0] == "POST"][0]
    assert stop["type"] == "stop" and stop["stop_price"] == "90.66" and stop["side"] == "sell"


async def test_losing_the_stop_and_failing_to_replace_it_is_loud():
    no_stop = [{"id": "T2", "type": "limit", "side": "sell", "qty": "144", "status": "new",
                "limit_price": "91.31"}]
    b, _ = _broker(_close_routes({
        ("GET", "/v2/orders"): [(200, _REAL)] + [(200, no_stop)] * 5,
        ("POST", "/v2/orders"): (500, {"message": "down"}),
    }))
    with pytest.raises(RuntimeError, match="CRITICAL"):
        await b.submit(_close(symbol="EWJ", qty=144))


async def test_no_position_wrong_direction_and_partial_close_are_refused_without_touching_anything():
    b, f = _broker({("GET", "/v2/positions/EWJ"): (404, {"message": "position does not exist"})})
    with pytest.raises(RuntimeError, match="no_position"):
        await b.submit(_close(symbol="EWJ"))
    b, f = _broker(_close_routes())
    with pytest.raises(RuntimeError, match="no_position"):  # BUY would ADD to a long
        await b.submit(_close(symbol="EWJ", side=Side.BUY, qty=144))
    b, f = _broker(_close_routes())
    with pytest.raises(RuntimeError, match="unsupported"):  # 50 of 144
        await b.submit(_close(symbol="EWJ", qty=50))
    assert _kinds(f, "DELETE", "POST", "PATCH") == []


def test_oms_treats_unsupported_and_no_position_as_terminal_not_retryable():
    from execution.oms import TERMINAL_ERR, _classify

    assert {"no_position", "unsupported"} <= TERMINAL_ERR
    assert _classify(RuntimeError("no_position: nothing to close")) == "no_position"
    assert _classify(RuntimeError("unsupported: reduce-only")) == "unsupported"
    # a transient failure of the replace must be RETRIED, not rejected for good
    assert _classify(AlpacaError(503, "down")) == "5xx"
    assert _classify(RuntimeError("timeout: no current price")) == "timeout"


async def test_live_exit_orders_include_held_stops_but_not_legs_of_unfilled_entries():
    listed = [
        # filled bracket: both legs count (the stop is "held")
        {"id": "P1", "order_class": "bracket", "status": "filled", "symbol": "EWJ", "type": "market",
         "side": "buy", "legs": [
             {"id": "T1", "symbol": "EWJ", "type": "limit", "side": "sell", "qty": "144",
              "status": "new", "limit_price": "128.52"},
             {"id": "S1", "symbol": "EWJ", "type": "stop", "side": "sell", "qty": "144",
              "status": "held", "stop_price": "90.66"}]},
        # entry still queued: its held legs protect nothing yet and must NOT look like orphans
        {"id": "P2", "order_class": "bracket", "status": "accepted", "symbol": "XLK",
         "type": "market", "side": "buy", "legs": [
             {"id": "T2", "symbol": "XLK", "type": "limit", "side": "sell", "qty": "50",
              "status": "held", "limit_price": "200"},
             {"id": "S2", "symbol": "XLK", "type": "stop", "side": "sell", "qty": "50",
              "status": "held", "stop_price": "150"}]},
        # an OCO placed after an incident: the target is top level, the stop is its leg
        {"id": "O1", "order_class": "oco", "status": "accepted", "symbol": "EWU", "type": "limit",
         "side": "sell", "qty": "351", "limit_price": "61.39", "legs": [
             {"id": "O2", "symbol": "EWU", "type": "stop", "side": "sell", "qty": "351",
              "status": "held", "stop_price": "45.77"}]},
        # finished legs and a finished bracket are ignored
        {"id": "P0", "order_class": "bracket", "status": "filled", "symbol": "SPY", "type": "market",
         "side": "buy", "legs": [
             {"id": "X1", "symbol": "SPY", "type": "stop", "side": "sell", "qty": "9",
              "status": "filled", "stop_price": "1"}]},
    ]
    b, _ = _broker({("GET", "/v2/orders"): (200, listed)})
    live = await b.live_exit_orders()
    assert sorted(live) == ["EWJ", "EWU"]
    assert sorted(o["id"] for o in live["EWJ"]) == ["S1", "T1"]
    assert sorted(o["id"] for o in live["EWU"]) == ["O1", "O2"]


async def test_take_profit_not_amendable_yet_is_a_wait_not_a_failure_and_touches_nothing():
    """The 29 Oct finding: an OCO placed after hours is 'accepted' and Alpaca refuses to amend it
    until the session opens. The close must wait (retryable) and change nothing."""
    accepted = [{"id": "O1", "order_class": "oco", "type": "limit", "side": "sell", "qty": "144",
                 "status": "accepted", "limit_price": "128.52", "symbol": "EWJ", "legs": [
                     {"id": "O2", "type": "stop", "side": "sell", "qty": "144", "status": "held",
                      "stop_price": "90.66", "symbol": "EWJ"}]}]
    b, f = _broker(_close_routes({("GET", "/v2/orders"): (200, accepted)}))
    with pytest.raises(RuntimeError, match="timeout"):
        await b.submit(_close(symbol="EWJ", qty=144))
    assert _kinds(f, "DELETE", "POST", "PATCH") == []


async def test_alpaca_refusing_the_amend_is_also_treated_as_retryable():
    b, f = _broker(_close_routes({("PATCH", "/v2/orders/T1"): (
        422, {"code": 42210000, "message": "cannot replace order in accepted status"})}))
    from execution.oms import _classify

    with pytest.raises(RuntimeError, match="timeout") as ei:
        await b.submit(_close(symbol="EWJ", qty=144))
    assert _classify(ei.value) == "timeout"  # the OMS will retry, not reject
    assert _kinds(f, "DELETE", "POST") == []
