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
    """Routes (method, path) -> (status, json) or a list consumed in order."""

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
    return b, fake


_LONG = {"qty": "10", "side": "long", "qty_available": "0"}
_FREE = {"qty": "10", "side": "long", "qty_available": "10"}
_LEGS = [
    {"id": "S1", "type": "stop", "side": "sell", "qty": "10", "stop_price": "95.00"},
    {"id": "T1", "type": "limit", "side": "sell", "qty": "10", "limit_price": "110.00"},
]


async def test_close_cancels_legs_first_then_sends_a_market_order_without_bracket():
    b, f = _broker({
        ("GET", "/v2/positions/AAPL"): [(200, _LONG), (200, _LONG), (200, _FREE)],
        ("GET", "/v2/orders"): (200, _LEGS),
        ("DELETE", "/v2/orders/S1"): (200, None),
        ("DELETE", "/v2/orders/T1"): (200, None),
        ("POST", "/v2/orders"): (200, {"id": "X1"}),
    })
    assert await b.submit(_close()) == "X1"
    kinds = [(m, p) for m, p, _ in f.calls if m in ("DELETE", "POST")]
    assert kinds == [("DELETE", "/v2/orders/S1"), ("DELETE", "/v2/orders/T1"),
                     ("POST", "/v2/orders")]  # legs off BEFORE the close
    body = [c[2] for c in f.calls if c[0] == "POST"][0]
    assert body["side"] == "sell" and body["qty"] == "10" and body["type"] == "market"
    assert "order_class" not in body and "stop_loss" not in body and "take_profit" not in body
    assert body["client_order_id"] == "c"
    assert await b.submit(_close()) == "X1"  # idempotent: no second POST
    assert len([c for c in f.calls if c[0] == "POST"]) == 1


async def test_close_with_no_position_is_a_terminal_error_not_a_retry_loop():
    b, _ = _broker({("GET", "/v2/positions/AAPL"): (404, {"message": "position does not exist"})})
    with pytest.raises(RuntimeError, match="no_position"):
        await b.submit(_close())


async def test_close_in_the_wrong_direction_is_refused_so_it_cannot_flip_a_position():
    b, f = _broker({("GET", "/v2/positions/AAPL"): (200, _FREE)})
    with pytest.raises(RuntimeError, match="no_position"):
        await b.submit(_close(side=Side.BUY))  # would ADD to a long
    assert not [c for c in f.calls if c[0] in ("POST", "DELETE")]


async def test_close_quantity_is_capped_at_the_actual_position():
    b, f = _broker({
        ("GET", "/v2/positions/AAPL"): [(200, _LONG), (200, _FREE)],
        ("GET", "/v2/orders"): (200, []),
        ("POST", "/v2/orders"): (200, {"id": "X2"}),
    })
    await b.submit(_close(qty=99))
    assert [c[2] for c in f.calls if c[0] == "POST"][0]["qty"] == "10"


async def test_failed_close_restores_the_protective_orders():
    b, f = _broker({
        ("GET", "/v2/positions/AAPL"): [(200, _LONG), (200, _FREE)],
        ("GET", "/v2/orders"): (200, _LEGS),
        ("DELETE", "/v2/orders/S1"): (200, None),
        ("DELETE", "/v2/orders/T1"): (200, None),
        ("POST", "/v2/orders"): [(403, {"message": "trading halted"}), (200, {"id": "R1"})],
    })
    with pytest.raises(AlpacaError):
        await b.submit(_close())
    restore = [c[2] for c in f.calls if c[0] == "POST"][1]  # the second POST
    assert restore["order_class"] == "oco" and restore["side"] == "sell"
    assert restore["stop_loss"] == {"stop_price": "95.00"}
    assert restore["take_profit"] == {"limit_price": "110.00"}
    assert "AAPL" not in b._saved_protection  # restored, nothing left pending


async def test_failed_close_and_failed_restore_is_loud():
    b, _ = _broker({
        ("GET", "/v2/positions/AAPL"): [(200, _LONG), (200, _FREE)],
        ("GET", "/v2/orders"): (200, _LEGS),
        ("DELETE", "/v2/orders/S1"): (200, None),
        ("DELETE", "/v2/orders/T1"): (200, None),
        ("POST", "/v2/orders"): [(403, {"message": "halted"}), (500, {"message": "down"})],
    })
    with pytest.raises(RuntimeError, match="CRITICAL"):
        await b.submit(_close())


def test_oms_treats_unsupported_and_no_position_as_terminal_not_retryable():
    from execution.oms import TERMINAL_ERR, _classify

    assert {"no_position", "unsupported"} <= TERMINAL_ERR
    assert _classify(RuntimeError("no_position: nothing to close")) == "no_position"
    assert _classify(RuntimeError("unsupported: reduce-only")) == "unsupported"
