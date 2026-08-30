"""Event-driven backtester. Same core (bus, clock, portfolio, strategy engine,
risk manager) as the live engine - only the broker and the clock differ.

    bars -> bus.publish(MarketData)
              -> Portfolio.on_market_data      (mark to market)
              -> BacktestBroker                (bracket exits, then pending fills)
              -> StrategyEngine                (new signals)
                   -> RiskService              (Signal -> Order | Rejection)
                        -> BacktestBroker       (queue for next bar)
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from core.bus import EventBus
from core.clock import SimClock
from core.events import Fill, MarketData, Order, Rejection, Signal
from data.features.library import atr as _atr_feature
from execution.portfolio import Portfolio
from execution.risk_service import RiskService
from execution.strategy_engine import StrategyEngine
from research.broker_sim import BacktestBroker, TradeRecord
from research.costs import CostModel
from research.metrics import PerfStats, summarise
from risk.manager import MarketState, RiskConfig, RiskManager
from risk.sizing import SizingParams

_INTERVAL = {
    "1d": timedelta(days=1),
    "1h": timedelta(hours=1),
    "5m": timedelta(minutes=5),
    "1m": timedelta(minutes=1),
}
_PPY = {"1d": 252, "1h": 252 * 7, "5m": 252 * 78, "1m": 252 * 390}


@dataclass(slots=True)
class InstrumentSpec:
    value_per_point: float = 1.0
    lot_step: float = 1.0
    min_lot: float = 1.0
    max_lot: float | None = None
    margin_rate: float = 1.0


@dataclass(slots=True)
class BacktestConfig:
    starting_equity: float = 100_000.0
    interval: str = "1d"
    atr_window: int = 14
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostModel = field(default_factory=CostModel)
    instruments: dict[str, InstrumentSpec] = field(default_factory=dict)
    seed: int = 0
    flatten_at_end: bool = True


@dataclass(slots=True)
class BacktestResult:
    equity_curve: pd.Series
    trades: pd.DataFrame
    stats: PerfStats
    rejections: pd.DataFrame
    signals: int
    orders: int
    fills: int
    config: BacktestConfig

    def summary(self) -> str:
        s = self.stats
        return (
            f"trades={s.n_trades}  ret={s.total_return:+.2%}  CAGR={s.cagr:+.2%}  "
            f"Sharpe={s.sharpe:.2f}  maxDD={s.max_drawdown:.2%}  "
            f"PF={s.profit_factor:.2f}  exp/trade={s.expectancy:+.2f}  "
            f"({self.signals} signals, {self.orders} orders, {self.rejections.shape[0]} rejections)"
        )


class Backtester:
    def __init__(self, strategies_factory, cfg: BacktestConfig | None = None) -> None:
        """``strategies_factory`` -> list[Strategy]. A factory (not instances) so
        each run starts from clean strategy state - required for CPCV/WFO."""
        self.make_strategies = strategies_factory
        self.cfg = cfg or BacktestConfig()

    def _spec(self, symbol: str) -> InstrumentSpec:
        return self.cfg.instruments.get(symbol, InstrumentSpec())

    async def run(self, bars: list[MarketData]) -> BacktestResult:
        cfg = self.cfg
        bars = sorted(bars, key=lambda b: (b.ts, b.symbol))
        if not bars:
            raise ValueError("no bars to backtest")

        vpp = {s: self._spec(s).value_per_point for s in {b.symbol for b in bars}}
        clock = SimClock(bars[0].ts)
        counters = {"signal": 0, "order": 0, "fill": 0}

        def _count(ev):
            if isinstance(ev, Signal):
                counters["signal"] += 1
            elif isinstance(ev, Order):
                counters["order"] += 1
            elif isinstance(ev, Fill):
                counters["fill"] += 1

        bus = EventBus(error_hook=_err_hook)
        bus.subscribe_all(_count)

        portfolio = Portfolio(cfg.starting_equity, value_per_point=vpp, history_len=512)
        bus.subscribe(MarketData, portfolio.on_market_data)
        bus.subscribe(Fill, portfolio.on_fill)

        broker = BacktestBroker(bus, cfg.costs, seed=cfg.seed, value_per_point=vpp)

        risk_mgr = RiskManager(cfg.risk)
        # these wire themselves onto the bus in __init__; keep refs alive
        services = (
            RiskService(bus, risk_mgr, portfolio, self._market_state_fn(portfolio, clock)),
            StrategyEngine(bus, portfolio, self.make_strategies()),
        )
        assert services

        rejections: list[Rejection] = []
        bus.subscribe(Rejection, lambda r: rejections.append(r))

        last_bar: dict[str, MarketData] = {}
        for bar in bars:
            clock.advance_to(bar.ts)
            last_bar[bar.symbol] = bar
            await bus.publish(bar)

        if cfg.flatten_at_end:
            await broker.flatten_all(last_bar)

        return self._assemble(portfolio, broker, rejections, counters)

    # ------------------------------------------------------------------ #
    def _market_state_fn(self, portfolio: Portfolio, clock: SimClock):
        cfg = self.cfg
        interval = _INTERVAL.get(cfg.interval, timedelta(days=1))

        def fn(symbol: str) -> MarketState:
            hist = portfolio.history(symbol, cfg.atr_window * 3 + 5)
            spec = self._spec(symbol)
            atr_val = None
            spread = None
            price = 0.0
            last_ts = None
            if hist:
                last = hist[-1]
                price = last.price
                spread = last.spread
                last_ts = last.ts
                if len(hist) >= cfg.atr_window + 1:
                    df = pd.DataFrame(
                        {
                            "high": [b.high for b in hist],
                            "low": [b.low for b in hist],
                            "close": [b.close for b in hist],
                        }
                    )
                    a = _atr_feature(df, window=cfg.atr_window).iloc[-1]
                    atr_val = float(a) if pd.notna(a) else None
            return MarketState(
                last_bar_ts=last_ts,
                bar_interval=interval,
                spread=spread,
                atr=atr_val,
                price=price,
                data_gap_open=False,
                value_per_point=spec.value_per_point,
                sizing=SizingParams(
                    value_per_point=spec.value_per_point,
                    lot_step=spec.lot_step,
                    min_lot=spec.min_lot,
                    max_lot=spec.max_lot,
                ),
                margin_available=portfolio.equity / max(spec.margin_rate, 1e-9),
                margin_rate=spec.margin_rate,
                high_impact_event=False,
            )

        return fn

    def _assemble(self, portfolio, broker, rejections, counters) -> BacktestResult:
        eq = pd.Series(
            {ts: v for ts, v in portfolio.equity_curve}, name="equity"
        ).sort_index()
        eq = eq[~eq.index.duplicated(keep="last")]

        trades_df = _trades_to_df(broker.trades)
        rej_df = pd.DataFrame(
            [
                {"ts": r.ts, "code": r.reason_code, "origin": r.origin,
                 "symbol": r.symbol, "detail": r.detail}
                for r in rejections
            ]
        )
        exposure = _exposure(portfolio, eq)
        stats = summarise(
            eq, trades_df, exposure=exposure,
            periods_per_year=_PPY.get(self.cfg.interval, 252),
        )
        return BacktestResult(
            equity_curve=eq,
            trades=trades_df,
            stats=stats,
            rejections=rej_df,
            signals=counters["signal"],
            orders=counters["order"],
            fills=counters["fill"],
            config=self.cfg,
        )


def _err_hook(etype, handler, event, exc):
    # Backtest: surface handler bugs loudly rather than swallowing them.
    raise exc


def _trades_to_df(trades: list[TradeRecord]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(
            columns=["symbol", "strategy_id", "direction", "entry_ts", "exit_ts",
                     "entry_price", "exit_price", "qty", "pnl", "fees",
                     "r_multiple", "exit_reason"]
        )
    return pd.DataFrame([dataclasses.asdict(t) for t in trades])


def _exposure(portfolio: Portfolio, eq: pd.Series) -> float:
    if eq.empty:
        return 0.0
    # crude: fraction of fills-to-bars; refined exposure needs a position timeline
    return min(1.0, len(portfolio.fills) / max(1, len(eq)))
