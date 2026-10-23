from __future__ import annotations

import numpy as np

from core.events import Direction, Fill, MarketData, Signal
from data.features.registry import features_hash
from strategies.etf_reversion.config import EtfReversionConfig


def rsi_wilder(close: np.ndarray, window: int) -> float:
    """Wilder-smoothed RSI of the last value, seeded from the start of ``close``.
    Uses only the bars given, so it cannot look ahead."""
    d = np.diff(close)
    if len(d) < window:
        return float("nan")
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:window].mean(), dn[:window].mean()
    for i in range(window, len(d)):
        au = (au * (window - 1) + up[i]) / window
        ad = (ad * (window - 1) + dn[i]) / window
    if ad == 0:
        return 100.0
    return float(100.0 - 100.0 / (1.0 + au / ad))


class EtfReversion:
    """Buy sharp dips inside an uptrend; exit on recovery or after a time stop.

    Entry carries a bracket (wide ATR stop, distant target) as the safety net. The real
    exits are rule-based: a FLAT signal when the close recovers above its
    ``exit_window`` average or after ``max_hold_bars``, which the risk manager turns
    into a reduce-only close order. Pure given a seed, no look-ahead.
    """

    def __init__(self, config: EtfReversionConfig) -> None:
        self.config = config
        self.strategy_id = config.strategy_id
        self._bars_seen: dict[str, int] = {}
        self._last_entry_at: dict[str, int] = {}
        self._last_exit_at: dict[str, int] = {}
        self._fhash = features_hash(
            ["sma", "rsi", "atr"],
            {"sma": config.trend_window, "rsi": config.rsi_window, "atr": config.atr_window},
        )

    def on_market_data(self, event: MarketData, ctx) -> list[Signal]:
        cfg = self.config
        sym = event.symbol
        if cfg.symbols and sym not in cfg.symbols:
            return []
        n = self._bars_seen[sym] = self._bars_seen.get(sym, 0) + 1

        bars = ctx.history(sym, cfg.warmup + 2)
        if len(bars) < cfg.warmup:
            return []
        close = np.array([b.close if b.close is not None else b.price for b in bars], dtype=float)
        high = np.array([b.high for b in bars], dtype=float)
        low = np.array([b.low for b in bars], dtype=float)
        price = float(close[-1])
        sma_exit = float(close[-cfg.exit_window:].mean())
        pos = ctx.position(sym)

        if pos != 0.0:
            held = n - self._last_entry_at.get(sym, n)
            recovered = price > sma_exit if pos > 0 else price < sma_exit
            if not (recovered or held >= cfg.max_hold_bars):
                return []
            if n - self._last_exit_at.get(sym, -(10**9)) < cfg.resignal_gap_bars:
                return []  # a close is probably still in flight
            self._last_exit_at[sym] = n
            return [Signal(
                ts=event.ts, symbol=sym, direction=Direction.FLAT, strategy_id=self.strategy_id,
                conviction=cfg.conviction, features_hash=self._fhash, entry_hint=price,
                meta={"reason": "recovered" if recovered else "time_stop"},
            )]

        if n - self._last_entry_at.get(sym, -(10**9)) < cfg.resignal_gap_bars:
            return []
        w = cfg.atr_window
        prev_close = close[-w - 1:-1]
        tr = np.maximum.reduce([
            high[-w:] - low[-w:], np.abs(high[-w:] - prev_close), np.abs(low[-w:] - prev_close),
        ])
        atr = float(tr.mean())
        sma_trend = float(close[-cfg.trend_window:].mean())
        rsi = rsi_wilder(close[-60:], cfg.rsi_window)
        if not (np.isfinite(atr) and atr > 0 and np.isfinite(rsi)):
            return []

        if price > sma_trend and rsi < cfg.rsi_entry:
            direction = Direction.LONG
            stop = price - cfg.stop_atr_mult * atr
            target = price + cfg.stop_atr_mult * atr * cfg.target_rr
        elif cfg.allow_short and price < sma_trend and rsi > 100.0 - cfg.rsi_entry:
            direction = Direction.SHORT
            stop = price + cfg.stop_atr_mult * atr
            target = price - cfg.stop_atr_mult * atr * cfg.target_rr
        else:
            return []
        if stop <= 0 or target <= 0:
            return []

        self._last_entry_at[sym] = n
        return [Signal(
            ts=event.ts, symbol=sym, direction=direction, strategy_id=self.strategy_id,
            conviction=cfg.conviction, features_hash=self._fhash,
            entry_hint=price, stop_hint=stop, target_hint=target,
            meta={"rsi": round(rsi, 2), "rsi_entry": cfg.rsi_entry},
        )]

    def on_fill(self, event: Fill) -> None:  # notification only
        return None
