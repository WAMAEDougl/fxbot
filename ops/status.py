"""Daily status + watchdog helpers (design 7: observability).

Pure functions only (no network, no clock), so they are easy to test; the script
``scripts/daily_status.py`` gathers the facts and sends the message.

Why a separate process and not part of the engine: it must be able to tell you
the engine has stopped, and if the whole server dies the daily message simply
stops arriving, which is itself the signal.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

HEARTBEAT_STALE_S = 180.0  # the engine rewrites its health file about every 10 s


@dataclass(slots=True)
class RoundTrip:
    symbol: str
    pnl: float
    closed_at: str


def round_trips(fills: list[dict]) -> list[RoundTrip]:
    """Realised P&L per completed trade (position goes from non-zero back to zero), from
    fills with ``symbol, side ('buy'|'sell'), qty, price, ts``. Average-cost accounting,
    works for longs and shorts. A fill that flips through zero closes one trade and
    opens the next at the same price."""
    state: dict[str, list[float]] = {}  # symbol -> [signed qty, avg price, realised pnl]
    out: list[RoundTrip] = []
    for f in sorted(fills, key=lambda x: x["ts"]):
        sym, px = f["symbol"], float(f["price"])
        q = float(f["qty"]) * (1.0 if f["side"] == "buy" else -1.0)
        pos, avg, realised = state.get(sym, [0.0, 0.0, 0.0])
        if pos == 0.0 or (pos > 0) == (q > 0):  # opening or adding
            new = pos + q
            avg = (avg * abs(pos) + px * abs(q)) / abs(new)
            state[sym] = [new, avg, realised]
            continue
        closing = min(abs(q), abs(pos))
        realised += (px - avg) * closing * (1.0 if pos > 0 else -1.0)
        new = pos + q
        if abs(new) < 1e-9:  # fully closed
            out.append(RoundTrip(sym, realised, str(f["ts"])))
            state[sym] = [0.0, 0.0, 0.0]
        elif (new > 0) != (pos > 0):  # flipped: close one trade, open the remainder
            out.append(RoundTrip(sym, realised, str(f["ts"])))
            state[sym] = [new, px, 0.0]
        else:  # partially reduced
            state[sym] = [new, avg, realised]
    return out


@dataclass(slots=True)
class Health:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    heartbeat_age_s: float | None = None


def unprotected_positions(positions: list[dict], symbols_with_stop: set[str]) -> list[str]:
    """Open positions that have no live stop order, e.g. ``["EWJ 144 sh"]``. A position without
    a stop is the one condition the bracket rule exists to prevent, so it is alerted on its own."""
    return [f"{p['symbol']} {abs(p['qty']):g} sh" for p in positions
            if p["symbol"] not in symbols_with_stop and abs(p["qty"]) > 0]


def orphan_orders(positions: dict[str, float], live: dict[str, list[dict]]) -> list[str]:
    """Symbols with a live exit order that would not REDUCE a position that exists: e.g. a stop
    left behind after its position closed, which could later open an unwanted position."""
    out = []
    for sym, legs in live.items():
        pos = positions.get(sym, 0.0)
        for leg in legs:
            reduces = (pos > 0 and leg["side"] == "sell") or (pos < 0 and leg["side"] == "buy")
            if not reduces:
                out.append(f"{sym} {leg['type']} {leg['side']} {leg['qty']}")
    return out


def engine_health(now_epoch: float, heartbeat_epoch: float | None, kill_switch_on: bool,
                  container_running: bool | None = None,
                  unprotected: list[str] | None = None,
                  orphans: list[str] | None = None) -> Health:
    """Judge the engine from its heartbeat file, the kill-switch flag, (optionally) whether its
    container is running, and any open position that has no stop order."""
    reasons: list[str] = []
    if orphans:
        reasons.append("ORPHAN ORDER (live exit order with no position to reduce): "
                       + ", ".join(orphans))
    if unprotected:
        reasons.append("UNPROTECTED POSITION (no stop order): " + ", ".join(unprotected))
    age = None if heartbeat_epoch is None else now_epoch - heartbeat_epoch
    if container_running is False:
        reasons.append("engine container is not running")
    if age is None:
        reasons.append("no heartbeat file")
    elif age > HEARTBEAT_STALE_S:
        reasons.append(f"heartbeat is {age:,.0f}s old (limit {HEARTBEAT_STALE_S:.0f}s)")
    if kill_switch_on:
        reasons.append("KILL SWITCH IS ENGAGED (no new orders)")
    return Health(ok=not reasons, reasons=reasons, heartbeat_age_s=age)


def _money(x: float, sign: bool = False) -> str:
    s = f"{abs(x):,.2f}"
    return (("+" if x >= 0 else "-") if sign else ("-" if x < 0 else "")) + "$" + s


@dataclass(slots=True)
class StatusFacts:
    env: str
    when: datetime
    health: Health
    equity: float
    last_equity: float
    cash: float
    positions: list[dict] = field(default_factory=list)  # symbol, qty, avg_entry, unrealized_pl, plpc
    open_orders: int = 0
    orders_24h: int = 0
    fills_24h: list[dict] = field(default_factory=list)
    closed_trades: list[RoundTrip] = field(default_factory=list)
    forward_start: str | None = None
    closest: list[tuple[str, float]] = field(default_factory=list)  # (symbol, rsi) in uptrend
    rsi_entry: float | None = None


def format_status(f: StatusFacts) -> str:
    h = f.health
    day = f.equity - f.last_equity
    day_pct = (day / f.last_equity * 100.0) if f.last_equity else 0.0
    head = f"[{f.env}] fxbot daily status, {f.when:%a %d %b %H:%M} UTC"
    eng = ("Engine: OK" + (f" (heartbeat {h.heartbeat_age_s:,.0f}s ago)"
                           if h.heartbeat_age_s is not None else ""))
    if not h.ok:
        eng = "PROBLEM: " + "; ".join(h.reasons)
    lines = [head, eng,
             f"Equity {_money(f.equity)} (day {_money(day, True)}, {day_pct:+.2f}%), "
             f"cash {_money(f.cash)}"]
    if f.positions:
        lines.append(f"Open positions ({len(f.positions)}):")
        for p in f.positions[:12]:
            lines.append(f"  {p['symbol']} {p['qty']:g} @ {p['avg_entry']:.2f}  "
                         f"{_money(p['unrealized_pl'], True)} ({p['plpc'] * 100:+.1f}%)")
        if len(f.positions) > 12:
            lines.append(f"  ... and {len(f.positions) - 12} more")
    else:
        lines.append("Open positions: none")
    lines.append(f"Open orders: {f.open_orders}. Last 24h: {f.orders_24h} orders sent, "
                 f"{len(f.fills_24h)} fills")
    for x in f.fills_24h[:8]:
        lines.append(f"  {x['side'].upper()} {x['qty']:g} {x['symbol']} @ {x['price']:.2f}")
    if f.forward_start:
        n = len(f.closed_trades)
        wins = sum(1 for t in f.closed_trades if t.pnl > 0)
        total = sum(t.pnl for t in f.closed_trades)
        wr = f"{wins / n * 100:.0f}%" if n else "n/a"
        lines.append(f"Forward test since {f.forward_start[:10]}: {n} closed trades, "
                     f"win rate {wr}, realised {_money(total, True)} (review at 100 trades / 3 months)")
    if f.closest and f.rsi_entry is not None:
        near = ", ".join(f"{s} RSI {r:.0f}" for s, r in f.closest[:3])
        lines.append(f"Closest to a buy (uptrend, RSI must fall below {f.rsi_entry:g}): {near}")
    lines.append("Orders sent after the close fill at the next open.")
    return "\n".join(lines)


def format_alert(env: str, when: datetime, health: Health) -> str:
    return (f"[{env}] fxbot WATCHDOG {when:%a %d %b %H:%M} UTC\n"
            "PROBLEM: " + "; ".join(health.reasons))


def send_telegram(text: str, token: str, chat_id: str, timeout: float = 15.0) -> None:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text[:4000]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        if resp.status != 200:
            raise RuntimeError(f"telegram returned HTTP {resp.status}")
