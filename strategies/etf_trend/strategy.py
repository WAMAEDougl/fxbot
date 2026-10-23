from __future__ import annotations

import numpy as np

from core.events import Direction, Fill, MarketData, Signal
from data.features.registry import features_hash
from strategies.etf_trend.config import EtfTrendConfig


class EtfTrend:
    """Be long while close > SMA(lookback), short while below (if allowed).

    Entries carry a bracket (stop at ``stop_atr_mult`` x ATR, distant target). With
    ``exit_on_trend_flip`` the strategy also emits a FLAT signal when the trend turns
    against an open position; the risk manager turns that into a reduce-only close.
    Without it, the bracket is the only exit. After any exit the strategy re-enters
    if the trend state still holds. Pure given a seed: same bars in, same signals
    out, no look-ahead (uses only bars up to and including this one).
    """

    def __init__(self, config: EtfTrendConfig) -> None:
        self.config = config
        self.strategy_id = config.strategy_id
        self._bars_seen: dict[str, int] = {}
        self._last_signal_at: dict[str, int] = {}  # entries
        self._last_exit_at: dict[str, int] = {}
        self._fhash = features_hash(
            ["sma", "atr"], {"sma": config.lookback, "atr": config.atr_window}
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
        w = cfg.atr_window
        sma = close[-cfg.lookback:].mean()
        prev_close = close[-w - 1:-1]
        tr = np.maximum.reduce([
            high[-w:] - low[-w:],
            np.abs(high[-w:] - prev_close),
            np.abs(low[-w:] - prev_close),
        ])
        atr = float(tr.mean())
        price = float(close[-1])
        if not np.isfinite(sma) or not np.isfinite(atr) or atr <= 0:
            return []

        pos = ctx.position(sym)
        if pos != 0.0:
            # in a position: the bracket manages the stop; optionally exit on a trend flip
            against = (pos > 0 and price < sma) or (pos < 0 and price > sma)
            if not (cfg.exit_on_trend_flip and against):
                return []
            if n - self._last_exit_at.get(sym, -(10**9)) < cfg.resignal_gap_bars:
                return []  # a close is probably still in flight
            self._last_exit_at[sym] = n
            return [Signal(
                ts=event.ts, symbol=sym, direction=Direction.FLAT, strategy_id=self.strategy_id,
                conviction=cfg.conviction, features_hash=self._fhash, entry_hint=price,
                meta={"reason": "trend_flip", "lookback": cfg.lookback},
            )]
        if n - self._last_signal_at.get(sym, -(10**9)) < cfg.resignal_gap_bars:
            return []

        if price > sma:
            direction = Direction.LONG
            stop = price - cfg.stop_atr_mult * atr
            target = price + cfg.stop_atr_mult * atr * cfg.target_rr
        elif cfg.allow_short:
            direction = Direction.SHORT
            stop = price + cfg.stop_atr_mult * atr
            target = price - cfg.stop_atr_mult * atr * cfg.target_rr
        else:
            return []
        if stop <= 0 or target <= 0:
            return []

        self._last_signal_at[sym] = n
        return [Signal(
            ts=event.ts, symbol=sym, direction=direction, strategy_id=self.strategy_id,
            conviction=cfg.conviction, features_hash=self._fhash,
            entry_hint=price, stop_hint=stop, target_hint=target,
            meta={"lookback": cfg.lookback, "stop_atr_mult": cfg.stop_atr_mult},
        )]

    def on_fill(self, event: Fill) -> None:  # notification only
        return None
