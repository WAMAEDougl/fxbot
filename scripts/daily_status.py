"""Send the daily status (and the hourly watchdog) to Telegram.

    python -m scripts.daily_status --dry-run                 # print, do not send
    python -m scripts.daily_status                           # daily status message
    python -m scripts.daily_status --only-if-unhealthy       # watchdog: silent unless broken

Runs as its own process (systemd timers, see deploy/) so it can report the engine being
down. Reads the engine's heartbeat file, the kill-switch flag, the engine's journal and
the broker. Environment: TS_ENV, TS_STATE_DIR, TS_KILLSWITCH_FILE, TS_CONFIG,
ALPACA_API_KEY/SECRET, TELEGRAM_BOT_TOKEN/CHAT_ID (see .env.example).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import yaml

from data.adapters.alpaca import LIVE_URL, PAPER_URL, AlpacaFeed, AlpacaHttp
from execution.brokers.alpaca import AlpacaBroker
from ops.status import (
    StatusFacts,
    engine_health,
    format_alert,
    format_status,
    orphan_orders,
    round_trips,
    send_telegram,
    unprotected_positions,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _heartbeat(path: Path) -> float | None:
    try:
        return float(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        try:
            return path.stat().st_mtime
        except OSError:
            return None


def _orders_24h(journal: Path, cutoff: datetime) -> int:
    if not journal.exists():
        return 0
    con = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT COUNT(DISTINCT key) FROM journal WHERE kind='order' AND ts >= ?",
            (cutoff.isoformat(),)).fetchone()
        return int(row[0])
    finally:
        con.close()


async def _fills(http: AlpacaHttp, base: str, after: datetime) -> list[dict]:
    out: list[dict] = []
    token = None
    for _ in range(20):  # paged; 100 per page
        page = await http.request(
            "GET", f"{base}/v2/account/activities/FILL",
            params={"after": _iso(after), "direction": "asc", "page_size": 100,
                    "page_token": token}) or []
        out += [{"symbol": a["symbol"], "side": "buy" if a["side"] == "buy" else "sell",
                 "qty": float(a["qty"]),
                 "price": float(a["price"]), "ts": a["transaction_time"]} for a in page]
        if len(page) < 100:
            break
        token = page[-1]["id"]
    return out


async def _closest(spec: dict) -> tuple[list[tuple[str, float]], float | None]:
    """For the RSI(2) dip strategy: the symbols in an uptrend with the lowest RSI, i.e. the
    nearest to a buy signal. Returns ([], None) for other strategies."""
    p = spec.get("params", {})
    if "rsi_entry" not in p or "trend_window" not in p:
        return [], None
    from strategies.etf_reversion.strategy import rsi_wilder

    feed, end, rows = AlpacaFeed(), datetime.now(UTC), []
    for s in p["symbols"]:
        bars = await feed.backfill(s, end - timedelta(days=int(p["trend_window"] * 2.2)), end, "1d")
        if len(bars) < p["trend_window"] + 5:
            continue
        c = np.array([b.close for b in bars], dtype=float)
        if c[-1] > c[-int(p["trend_window"]):].mean():
            rows.append((s, rsi_wilder(c[-60:], int(p.get("rsi_window", 2)))))
    return sorted(rows, key=lambda r: r[1]), float(p["rsi_entry"])


async def _protection_problems(env: str) -> tuple[list[str], list[str]]:
    """(positions with no live stop, live exit orders with no position to reduce). If the broker
    cannot be read, report nothing: a network blip must not raise a false alarm, and the next
    run checks again."""
    try:
        broker = AlpacaBroker(env=env)
        positions = await broker.positions()
        live = await broker.live_exit_orders()
        with_stop = {sym for sym, legs in live.items()
                     for o in legs if o["type"] in ("stop", "stop_limit")
                     and o["side"] == ("sell" if positions.get(sym, 0.0) > 0 else "buy")}
        unprot = unprotected_positions(
            [{"symbol": s_, "qty": q} for s_, q in positions.items()], with_stop)
        return unprot, orphan_orders(positions, live)
    except Exception as exc:  # noqa: BLE001
        print(f"could not check protection: {exc!r}", file=sys.stderr)
        return [], []


async def _run(args) -> int:
    env = os.environ.get("TS_ENV", "paper")
    state = Path(os.environ.get("TS_STATE_DIR", "state"))
    now = datetime.now(UTC)
    kill = Path(os.environ.get("TS_KILLSWITCH_FILE", str(state / "HALT"))).exists()
    unprot, orphans = await _protection_problems(env)
    health = engine_health(time.time(), _heartbeat(state / "healthy"), kill,
                           unprotected=unprot, orphans=orphans)

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")

    def deliver(text: str) -> int:
        if args.dry_run:
            print(text)
            return 0
        if not (token and chat):
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
            return 2
        send_telegram(text, token, chat)
        print(f"sent {len(text)} chars")
        return 0

    if args.only_if_unhealthy:
        if health.ok:
            print("engine healthy; nothing to send")
            return 0
        return deliver(format_alert(env, now, health))

    spec = yaml.safe_load(Path(args.config).read_text()) if args.config else {}
    base = PAPER_URL if env == "paper" else LIVE_URL
    http = AlpacaHttp()
    acct = await http.request("GET", f"{base}/v2/account")
    positions = []
    for p in await http.request("GET", f"{base}/v2/positions") or []:
        qty = abs(float(p["qty"])) * (-1.0 if p.get("side") == "short" else 1.0)
        positions.append({"symbol": p["symbol"], "qty": qty,
                          "avg_entry": float(p["avg_entry_price"]),
                          "unrealized_pl": float(p["unrealized_pl"]),
                          "plpc": float(p["unrealized_plpc"])})
    open_orders = len(await http.request(
        "GET", f"{base}/v2/orders", params={"status": "open", "limit": 100}) or [])

    fwd = (spec.get("forward_test") or {}).get("start")
    since = now - timedelta(hours=24)
    closed = []
    if fwd:  # report only this test's activity, not earlier pipeline tests
        start = datetime.fromisoformat(fwd.replace("Z", "+00:00"))
        since = max(since, start)
        closed = round_trips(await _fills(http, base, start))
    fills_24h = await _fills(http, base, since)
    closest, rsi_entry = await _closest(spec) if spec else ([], None)

    facts = StatusFacts(
        env=env, when=now, health=health, equity=float(acct["equity"]),
        last_equity=float(acct["last_equity"]), cash=float(acct["cash"]), positions=positions,
        open_orders=open_orders, orders_24h=_orders_24h(state / f"journal-{env}.sqlite",
                                                        now - timedelta(hours=24)),
        fills_24h=fills_24h, closed_trades=closed, forward_start=fwd, closest=closest,
        rsi_entry=rsi_entry)
    return deliver(format_status(facts))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=os.environ.get("TS_CONFIG"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only-if-unhealthy", action="store_true")
    sys.exit(asyncio.run(_run(p.parse_args())))


if __name__ == "__main__":
    main()
