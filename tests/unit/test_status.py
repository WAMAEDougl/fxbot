from datetime import UTC, datetime

from ops.status import (
    Health,
    RoundTrip,
    StatusFacts,
    engine_health,
    format_alert,
    format_status,
    round_trips,
)

NOW = datetime(2026, 10, 24, 5, 30, tzinfo=UTC)


def _f(sym, side, qty, px, ts):
    return {"symbol": sym, "side": side, "qty": qty, "price": px, "ts": ts}


# ------------------------------------------------------------ round trips #
def test_simple_long_round_trip():
    rt = round_trips([_f("A", "buy", 10, 100, "1"), _f("A", "sell", 10, 103, "2")])
    assert len(rt) == 1 and abs(rt[0].pnl - 30.0) < 1e-9


def test_loss_and_partial_fills_aggregate_into_one_trade():
    rt = round_trips([
        _f("A", "buy", 6, 100, "1"), _f("A", "buy", 4, 110, "2"),  # avg 104
        _f("A", "sell", 5, 100, "3"), _f("A", "sell", 5, 100, "4"),  # -4 * 10 = -40
    ])
    assert len(rt) == 1 and abs(rt[0].pnl + 40.0) < 1e-9


def test_trade_is_not_counted_until_flat():
    assert round_trips([_f("A", "buy", 10, 100, "1"), _f("A", "sell", 4, 110, "2")]) == []


def test_symbols_are_independent_and_ordering_is_by_time():
    rt = round_trips([
        _f("B", "sell", 5, 50, "4"), _f("A", "sell", 10, 101, "3"),
        _f("A", "buy", 10, 100, "1"), _f("B", "buy", 5, 40, "2"),
    ])
    assert {t.symbol: round(t.pnl, 6) for t in rt} == {"A": 10.0, "B": 50.0}


def test_short_round_trip_profits_when_price_falls():
    rt = round_trips([_f("A", "sell", 10, 100, "1"), _f("A", "buy", 10, 95, "2")])
    assert abs(rt[0].pnl - 50.0) < 1e-9


def test_flipping_through_zero_closes_one_trade_and_opens_the_next():
    rt = round_trips([_f("A", "buy", 10, 100, "1"), _f("A", "sell", 15, 110, "2"),
                      _f("A", "buy", 5, 105, "3")])
    assert len(rt) == 2 and abs(rt[0].pnl - 100.0) < 1e-9 and abs(rt[1].pnl - 25.0) < 1e-9


# ----------------------------------------------------------------- health #
def test_healthy_engine():
    h = engine_health(1000.0, 995.0, kill_switch_on=False, container_running=True)
    assert h.ok and h.reasons == [] and h.heartbeat_age_s == 5.0


def test_stale_heartbeat_is_a_problem():
    h = engine_health(1000.0, 1000.0 - 600, False, True)
    assert not h.ok and "heartbeat" in h.reasons[0]


def test_missing_heartbeat_stopped_container_and_kill_switch_all_reported():
    h = engine_health(1000.0, None, True, False)
    text = " | ".join(h.reasons)
    assert not h.ok and "container" in text and "no heartbeat" in text and "KILL SWITCH" in text


# ------------------------------------------------------------- formatting #
def _facts(**kw):
    base = dict(env="paper", when=NOW, health=Health(True, [], 8.0), equity=99_709.73,
                last_equity=100_000.0, cash=99_709.73)
    base.update(kw)
    return StatusFacts(**base)


def test_quiet_day_message_says_so_plainly():
    msg = format_status(_facts(forward_start="2026-10-23T12:11:00Z", rsi_entry=15.0,
                               closest=[("XLK", 21.4), ("EWJ", 18.4), ("XLE", 28.0)]))
    assert "Engine: OK" in msg and "Open positions: none" in msg
    assert "0 orders sent, 0 fills" in msg and "0 closed trades, win rate n/a" in msg
    assert "XLK RSI 21" in msg and "day -$290.27" in msg
    assert len(msg) < 4000


def test_active_day_lists_positions_fills_and_trade_stats():
    msg = format_status(_facts(
        positions=[{"symbol": "XLK", "qty": 50.0, "avg_entry": 182.34,
                    "unrealized_pl": 120.5, "plpc": 0.0132}],
        open_orders=1, orders_24h=2,
        fills_24h=[{"side": "buy", "qty": 50.0, "symbol": "XLK", "price": 182.34}],
        forward_start="2026-10-23T12:11:00Z",
        closed_trades=[RoundTrip("A", 50.0, "x"), RoundTrip("B", -20.0, "y")]))
    assert "XLK 50 @ 182.34" in msg and "+$120.50" in msg
    assert "BUY 50 XLK @ 182.34" in msg
    assert "2 closed trades, win rate 50%, realised +$30.00" in msg


def test_unhealthy_status_leads_with_the_problem():
    bad = Health(False, ["heartbeat is 900s old (limit 180s)"], 900.0)
    assert "ENGINE PROBLEM: heartbeat is 900s old" in format_status(_facts(health=bad))
    alert = format_alert("paper", NOW, bad)
    assert "WATCHDOG" in alert and "ENGINE PROBLEM" in alert
