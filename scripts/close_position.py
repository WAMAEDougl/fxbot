"""Close one open position safely, through the same OMS and Alpaca adapter the bot uses.

    python -m scripts.close_position --symbol EWJ [--timeout-hours 10]

Sells the position THROUGH its own take-profit leg (the stop stays live until the sale fills,
then Alpaca cancels it), retrying patiently if the broker cannot amend the order yet (for
example an order placed after hours is not amendable until the session opens). Prints each
status change and a final report. Paper by default (TS_ENV); a live account needs --confirm-live.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

from core.bus import EventBus
from core.clock import LiveClock
from core.events import Order, OrderType, Rejection, Side
from execution.brokers.alpaca import AlpacaBroker
from execution.oms import OMS, OrderStatus


def _log(msg: str) -> None:
    print(f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S}Z  {msg}", flush=True)


async def _run(args) -> int:
    env = os.environ.get("TS_ENV", "paper")
    if env == "live" and not args.confirm_live:
        raise SystemExit("closing on a LIVE account requires --confirm-live")
    sym = args.symbol.upper()
    broker = AlpacaBroker(env=env)

    qty = (await broker.positions()).get(sym, 0.0)
    if qty == 0.0:
        _log(f"no open position in {sym}; nothing to do")
        return 0
    side = Side.SELL if qty > 0 else Side.BUY
    _log(f"{sym}: {qty:g} open; closing with a {side.value} through its take-profit leg")

    bus, rejections = EventBus(), []
    bus.subscribe(Rejection, rejections.append)
    oms = OMS(bus, broker, LiveClock())
    now = datetime.now(UTC)
    coid = f"manual-close-{sym}-{now:%Y%m%d%H%M%S}"
    await bus.publish(Order(
        ts=now, client_order_id=coid, symbol=sym, side=side, qty=abs(qty),
        order_type=OrderType.MARKET, strategy_id="manual", reduce_only=True))

    deadline, after, last = now + timedelta(hours=args.timeout_hours), now - timedelta(minutes=2), None
    while datetime.now(UTC) < deadline:
        for fill in await broker.poll_fills(after):
            after = max(after, fill.ts)
            await oms.on_broker_fill(fill)
        st = oms.state(coid)
        line = f"{st.status.value} attempts={st.attempts} filled={st.filled_qty:g} err={st.last_error or '-'}"
        if line != last:
            _log(line)
            last = line
        if st.status in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED):
            break
        await asyncio.sleep(args.poll_s)

    st = oms.state(coid)
    left = (await broker.positions()).get(sym, 0.0)
    live = await broker.live_exit_orders()
    _log(f"FINAL: order {st.status.value}, avg fill {st.avg_fill_price:.4f}, position left {left:g}")
    _log(f"live exit orders on {sym}: "
         f"{[(o['type'], o['side'], o['status']) for o in live.get(sym, [])] or 'none'}")
    for r in rejections:
        _log(f"rejection: {r.reason_code} {r.detail[:200]}")
    ok = st.status is OrderStatus.FILLED and left == 0.0 and not live.get(sym)
    _log("RESULT: " + ("OK, position closed and nothing left behind" if ok else "NEEDS ATTENTION"))
    return 0 if ok else 1


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True)
    p.add_argument("--timeout-hours", type=float, default=10.0)
    p.add_argument("--poll-s", type=float, default=30.0)
    p.add_argument("--confirm-live", action="store_true")
    sys.exit(asyncio.run(_run(p.parse_args())))


if __name__ == "__main__":
    main()
