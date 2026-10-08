"""Run a strategy against IBKR (paper or live).

    python -m scripts.run_live --config configs/ma_crossover.example.yaml

Environment (see .env.example): TS_BROKER (ibkr|alpaca), TS_ENV, then either
IB_HOST/IB_PORT/IB_ACCOUNT or ALPACA_API_KEY/ALPACA_API_SECRET, TS_STATE_DIR,
TS_KILLSWITCH_FILE, TELEGRAM_*, ... The engine halts new orders if
``$TS_KILLSWITCH_FILE`` exists (``touch`` to halt, delete to resume).

Safety: with IBKR, TS_ENV must agree with the IB port, otherwise the process
refuses to start. With Alpaca, TS_ENV picks the paper or live endpoint and
Alpaca rejects keys of the other kind.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from pathlib import Path

import yaml

from execution.live import LiveConfig, LiveRunner
from execution.persist import StateJournal
from ops.alerting import Alerter, Severity, console_transport, telegram_transport
from ops.killswitch import KillSwitch
from research.backtest import BacktestConfig, InstrumentSpec
from research.costs import CostModel
from risk.manager import RiskConfig
from scripts.run_backtest import _config_name, _load_strategy_class

_PAPER_PORTS = {7497, 4002, 4004}
_LIVE_PORTS = {7496, 4001, 4003}


def check_env_matches_port(env: str, port: int) -> None:
    if env not in ("paper", "live"):
        raise SystemExit(f"TS_ENV must be 'paper' or 'live', got {env!r}")
    if env == "paper" and port in _LIVE_PORTS:
        raise SystemExit(f"TS_ENV=paper but IB_PORT={port} is a LIVE port. Refusing to start.")
    if env == "live" and port in _PAPER_PORTS:
        raise SystemExit(f"TS_ENV=live but IB_PORT={port} is a PAPER port. Refusing to start.")


def _alerter() -> Alerter:
    transports = {s: [console_transport] for s in Severity}
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        tg = telegram_transport(token, chat)
        transports[Severity.WARNING].append(tg)
        transports[Severity.CRITICAL].append(tg)
    return Alerter(transports)


async def _run(args) -> None:
    env = os.environ.get("TS_ENV", "paper")
    kind = os.environ.get("TS_BROKER", "ibkr").lower()
    if kind not in ("ibkr", "alpaca"):
        raise SystemExit(f"TS_BROKER must be 'ibkr' or 'alpaca', got {kind!r}")
    port = int(os.environ.get("IB_PORT", "7497"))
    if kind == "ibkr":
        check_env_matches_port(env, port)
    elif env not in ("paper", "live"):
        raise SystemExit(f"TS_ENV must be 'paper' or 'live', got {env!r}")
    if env == "live" and not args.confirm_live:
        raise SystemExit("live trading requires --confirm-live")

    with open(args.config) as fh:
        spec = yaml.safe_load(fh)
    strat_cls = _load_strategy_class(spec["strategy_class"])
    params = spec["params"]
    cfg_cls = _load_strategy_class(spec["strategy_class"].split(":")[0] + ":" + _config_name(strat_cls))
    symbols = params["symbols"]
    bt = spec.get("backtest", {})
    interval = bt.get("interval", "1d")
    bt_cfg = BacktestConfig(
        starting_equity=0.0,  # replaced by broker equity at startup
        interval=interval,
        risk=RiskConfig(**bt.get("risk", {})),
        costs=CostModel(**bt.get("costs", {})),
        instruments={s: InstrumentSpec(**v) for s, v in bt.get("instruments", {}).items()},
    )
    strategy = strat_cls(cfg_cls(**params))

    state_dir = Path(os.environ.get("TS_STATE_DIR", "state"))
    host = os.environ.get("IB_HOST", "127.0.0.1")
    client_id = int(os.environ.get("IB_CLIENT_ID", "21"))
    if kind == "alpaca":
        from data.adapters.alpaca import AlpacaFeed, AlpacaHttp
        from execution.brokers.alpaca import AlpacaBroker

        http = AlpacaHttp()
        broker, feed = AlpacaBroker(env=env, http=http), AlpacaFeed(http)
    else:
        from data.adapters.ibkr import IbkrFeed
        from execution.brokers.ibkr import IbkrBroker

        broker = IbkrBroker(host=host, port=port, client_id=client_id)
        feed = IbkrFeed(host=host, port=port, client_id=client_id + 1000)
    runner = LiveRunner(
        LiveConfig(
            env=env, symbols=symbols, interval=interval,
            warmup_bars=getattr(strategy.config, "warmup", 120),
            reconcile_interval_s=float(os.environ.get("TS_RECONCILE_INTERVAL_S", "300")),
            health_file=state_dir / "healthy", feed_name=kind,
        ),
        broker=broker,
        feed=feed,
        strategies=[strategy],
        bt_cfg=bt_cfg,
        journal=StateJournal(state_dir / f"journal-{env}.sqlite"),
        killswitch=KillSwitch(
            os.environ.get("TS_KILLSWITCH_FILE", str(state_dir / "HALT")),
            daily_loss_limit=float(os.environ.get("TS_DAILY_LOSS_LIMIT", "0.03")),
            max_rejections_per_window=int(os.environ.get("TS_MAX_REJECTIONS_PER_5MIN", "5")),
            feed_stale_after_s=float(os.environ.get("TS_FEED_STALE_AFTER_S", "180")),
        ),
        alerter=_alerter(),
        reconcile_qty_tolerance=float(os.environ.get("TS_RECONCILE_QTY_TOLERANCE", "1e-6")),
        reconcile_equity_tolerance_frac=float(
            os.environ.get("TS_RECONCILE_EQUITY_TOLERANCE_FRAC", "0.005")
        ),
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runner.stop)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(runner.stop))

    await runner.start()
    await runner.run()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--confirm-live", action="store_true",
                   help="required when TS_ENV=live")
    asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    main()
