"""Live / paper runner (design sections 4, 6, 7).

Wires the same core the backtester uses (bus, portfolio, strategy engine, risk
service) to a real feed and broker:

    feed.backfill -> completed bars -> bus -> Portfolio / StrategyEngine
        -> RiskService -> OMS -> BrokerAdapter -> fills -> bus

Daily/swing horizon: bars are *polled* (``feed.backfill`` over the recent
window) and only completed bars are published, so a strategy never acts on a
half-formed bar. A per-symbol cursor is journalled, so a restart neither
replays old bars (which would re-signal) nor skips bars missed while down.

Startup order matters (design 4.5): broker truth first (equity, positions),
then warmup with signals discarded, then reconcile, only then trade.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from core.bus import EventBus
from core.clock import Clock, LiveClock
from core.events import Fill, MarketData, Rejection
from core.protocols import BrokerAdapter, Strategy
from execution.oms import OMS, OrderState
from execution.persist import StateJournal
from execution.portfolio import Portfolio
from execution.reconcile import Reconciler
from execution.risk_service import RiskService
from execution.strategy_engine import StrategyEngine
from ops.alerting import Alerter, Severity
from ops.killswitch import KillSwitch
from ops.metrics import MetricsCollector
from research.backtest import _INTERVAL, BacktestConfig, Backtester
from risk.manager import RiskManager


@dataclass(slots=True)
class LiveConfig:
    env: str  # "paper" | "live" - labels every alert and log line
    symbols: list[str]
    interval: str = "1d"
    warmup_bars: int = 120
    poll_interval_s: float = 60.0
    reconcile_interval_s: float = 300.0
    health_file: Path | None = None
    feed_name: str = "feed"
    max_consecutive_poll_failures: int = 3


class LiveRunner:
    def __init__(
        self,
        cfg: LiveConfig,
        *,
        broker: BrokerAdapter,
        feed,
        strategies: list[Strategy],
        bt_cfg: BacktestConfig,
        journal: StateJournal,
        killswitch: KillSwitch,
        alerter: Alerter,
        clock: Clock | None = None,
        reconcile_qty_tolerance: float = 1e-6,
        reconcile_equity_tolerance_frac: float = 0.005,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.feed = feed
        self.strategies = strategies
        self.bt_cfg = bt_cfg
        self.journal = journal
        self.ks = killswitch
        self.alerter = alerter
        self.clock = clock or LiveClock()
        self._recon_tols = (reconcile_qty_tolerance, reconcile_equity_tolerance_frac)

        self.bus = EventBus(error_hook=self._on_handler_error)
        self.portfolio: Portfolio | None = None
        self.oms: OMS | None = None
        self.reconciler: Reconciler | None = None
        self.metrics: MetricsCollector | None = None
        self._cursor: dict[str, datetime] = {}
        self._stop = asyncio.Event()
        self._poll_failures = 0
        self._ks_alerted = False
        self._day: datetime | None = None
        self._day_start_equity = 0.0

    # ------------------------------------------------------------ startup #
    async def start(self) -> None:
        acct = await self.broker.account()
        equity = acct.get("equity", 0.0)
        if equity <= 0:
            raise RuntimeError(f"broker reports non-positive equity ({equity}); refusing to start")

        vpp = {s: self.bt_cfg.instruments[s].value_per_point
               for s in self.cfg.symbols if s in self.bt_cfg.instruments}
        p = self.portfolio = Portfolio(equity, value_per_point=vpp, history_len=512)

        # Wiring order mirrors the backtester: portfolio sees data/fills first.
        self.bus.subscribe(MarketData, p.on_market_data)
        self.bus.subscribe(Fill, p.on_fill)
        market_state = Backtester(None, self.bt_cfg)._market_state_fn(p, self.clock)
        self._risk = RiskService(
            self.bus, RiskManager(self.bt_cfg.risk, kill_switch=self.ks), p, market_state
        )
        self._engine = StrategyEngine(
            self.bus, p, self.strategies, on_disable=self._on_strategy_disabled
        )
        self.oms = OMS(self.bus, self.broker, self.clock, persist=self._persist_order)
        self.bus.subscribe(Fill, self._journal_fill)
        self.bus.subscribe(Rejection, self._on_rejection)
        self.alerter.attach(self.bus)
        self.metrics = MetricsCollector(self.bus)
        self.reconciler = Reconciler(
            self.broker, p, self.clock,
            qty_tolerance=self._recon_tols[0],
            equity_tolerance_frac=self._recon_tols[1],
            on_divergence=self._on_divergence,
        )

        pending = await self._warmup()

        for sym, qty in (await self.broker.positions()).items():
            if qty:
                hist = p.history(sym, 1)
                price = hist[-1].price if hist else 0.0
                p.seed_position(sym, qty, price)

        res = await self.reconciler.run_once()
        if not res.ok:
            self.alerter.send(Severity.CRITICAL, f"[{self.cfg.env}] startup reconcile FAILED",
                              f"{res.divergences} equity_delta={res.equity_delta:.2f}")
        for bar in pending:  # bars that closed while we were down
            await self._publish_bar(bar)
        self._roll_day(equity)
        self.alerter.send(Severity.INFO, f"[{self.cfg.env}] runner started",
                          f"equity={equity:.2f} symbols={self.cfg.symbols}")

    async def _warmup(self) -> list[MarketData]:
        """Rebuild history and strategy state from bars, discarding signals.
        Returns bars newer than the journalled cursor (to be traded on)."""
        assert self.portfolio is not None
        now = self.clock.now()
        step = _INTERVAL.get(self.cfg.interval, timedelta(days=1))
        lookback = step * (self.cfg.warmup_bars * 2 + 10)  # *2: weekends/holidays
        pending: list[MarketData] = []
        for sym in self.cfg.symbols:
            saved = self.journal.latest("cursor", sym)
            saved_ts = datetime.fromisoformat(saved["ts"]) if saved else None
            bars = self._completed(await self.feed.backfill(sym, now - lookback, now,
                                                            self.cfg.interval), now)
            for b in bars:
                if saved_ts is not None and b.ts > saved_ts:
                    pending.append(b)
                    continue
                self.portfolio.on_market_data(b)
                for s in self.strategies:
                    s.on_market_data(b, self.portfolio)  # signals discarded
            if bars:
                self._set_cursor(sym, saved_ts if saved_ts else bars[-1].ts)
        pending.sort(key=lambda b: (b.ts, b.symbol))
        return pending

    # ----------------------------------------------------------- bar flow #
    def _completed(self, bars: list[MarketData], now: datetime) -> list[MarketData]:
        step = _INTERVAL.get(self.cfg.interval, timedelta(days=1))
        return sorted((b for b in bars if b.ts + step <= now), key=lambda b: b.ts)

    async def poll_once(self) -> int:
        """Publish bars that completed since the cursor. Returns how many."""
        now = self.clock.now()
        new: list[MarketData] = []
        for sym in self.cfg.symbols:
            cur = self._cursor.get(sym)
            start = (cur - timedelta(days=5)) if cur else now - timedelta(days=10)
            bars = self._completed(await self.feed.backfill(sym, start, now, self.cfg.interval),
                                   now)
            new += [b for b in bars if cur is None or b.ts > cur]
        self.ks.on_feed_tick(self.cfg.feed_name)  # only after every symbol succeeded
        for bar in sorted(new, key=lambda b: (b.ts, b.symbol)):
            await self._publish_bar(bar)
        return len(new)

    async def _publish_bar(self, bar: MarketData) -> None:
        await self.bus.publish(bar)
        self._set_cursor(bar.symbol, bar.ts)

    def _set_cursor(self, symbol: str, ts: datetime) -> None:
        self._cursor[symbol] = ts
        self.journal.append("cursor", symbol, {"ts": ts.isoformat()})

    # ----------------------------------------------------------- reconcile #
    async def reconcile_once(self) -> None:
        assert self.reconciler is not None
        await self.reconciler.run_once()
        equity = (await self.broker.account()).get("equity", 0.0)
        self._roll_day(equity)
        if self._day_start_equity > 0:
            self.ks.on_daily_loss((self._day_start_equity - equity) / self._day_start_equity)

    def _roll_day(self, equity: float) -> None:
        today = self.clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if self._day != today:
            self._day, self._day_start_equity = today, equity

    # -------------------------------------------------------------- hooks #
    def _persist_order(self, st: OrderState) -> None:
        self.journal.append("order", st.order.client_order_id, {
            "status": st.status.value, "broker_order_id": st.broker_order_id,
            "filled_qty": st.filled_qty, "avg_fill_price": st.avg_fill_price,
            "attempts": st.attempts, "last_error": st.last_error,
            "symbol": st.order.symbol, "qty": st.order.qty, "side": st.order.side.value,
            "strategy_id": st.order.strategy_id,
        })

    def _journal_fill(self, fill: Fill) -> None:
        self.journal.append("fill", fill.order_id, dataclasses.asdict(fill))

    def _on_rejection(self, r: Rejection) -> None:
        if r.origin in ("broker", "oms"):
            self.ks.on_rejection()

    def _on_divergence(self, divs, eq_delta: float) -> None:
        detail = f"{[(d.symbol, d.local_qty, d.broker_qty) for d in divs]} equity_delta={eq_delta:.2f}"
        self.ks.on_reconcile_divergence(detail)
        self.alerter.send(Severity.CRITICAL, f"[{self.cfg.env}] reconcile drift - halted", detail)

    def _on_strategy_disabled(self, sid: str, exc: BaseException) -> None:
        self.alerter.send(Severity.CRITICAL, f"[{self.cfg.env}] strategy {sid} disabled", repr(exc))

    def _on_handler_error(self, etype, handler, event, exc) -> None:
        # A crashing core handler (risk/OMS/portfolio) is not recoverable by
        # carrying on: stop opening, keep managing (brackets stay at the broker).
        self.ks.engage(f"handler error on {etype.__name__}: {exc!r}")
        self.alerter.send(Severity.CRITICAL, f"[{self.cfg.env}] handler error - halted",
                          f"{getattr(handler, '__qualname__', handler)}: {exc!r}")

    # ---------------------------------------------------------------- loop #
    async def run(self) -> None:
        loops = [self._poll_loop(), self._reconcile_loop(), self._fills_loop(), self._watch_loop()]
        tasks = [asyncio.create_task(c) for c in loops]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.alerter.send(Severity.INFO, f"[{self.cfg.env}] runner stopped", "")

    def stop(self) -> None:
        self._stop.set()

    async def _nap(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once()
                self._poll_failures = 0
            except Exception as exc:  # noqa: BLE001
                self._poll_failures += 1
                if self._poll_failures == self.cfg.max_consecutive_poll_failures:
                    self.alerter.send(Severity.CRITICAL, f"[{self.cfg.env}] feed failing",
                                      f"{self._poll_failures} consecutive poll failures: {exc!r}")
            await self._nap(self.cfg.poll_interval_s)

    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            await self._nap(self.cfg.reconcile_interval_s)
            try:
                await self.reconcile_once()
            except Exception as exc:  # noqa: BLE001
                self.alerter.send(Severity.WARNING, f"[{self.cfg.env}] reconcile error", repr(exc))

    async def _fills_loop(self) -> None:
        assert self.oms is not None
        while not self._stop.is_set():
            try:
                async for fill in self.broker.stream_fills():
                    await self.oms.on_broker_fill(fill)
            except Exception as exc:  # noqa: BLE001
                self.alerter.send(Severity.WARNING, f"[{self.cfg.env}] fill stream error", repr(exc))
            await self._nap(5.0)

    async def _watch_loop(self) -> None:
        while not self._stop.is_set():
            self.ks.check_feed()
            engaged = self.ks.engaged
            if engaged and not self._ks_alerted:
                self.alerter.send(Severity.CRITICAL, f"[{self.cfg.env}] KILL SWITCH ENGAGED",
                                  self.ks.state().reason)
            self._ks_alerted = engaged
            if self.cfg.health_file is not None:
                self.cfg.health_file.parent.mkdir(parents=True, exist_ok=True)
                self.cfg.health_file.write_text(f"{time.time():.0f}\n")
            await self._nap(10.0)

