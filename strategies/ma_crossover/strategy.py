from __future__ import annotations

import pandas as pd

from core.events import Direction, Fill, MarketData, Signal
from data.features.registry import compute, features_hash
from strategies.ma_crossover.config import MaCrossoverConfig

_FEATURES = ["sma", "atr"]


class MaCrossover:
    """Fast SMA over slow SMA -> long; fast under slow -> flat (or short).

    Deterministic: same bars in, same signals out. All inputs come from the
    causal feature store, so nothing here can see the future.
    """

    def __init__(self, config: MaCrossoverConfig) -> None:
        self.config = config
        self.strategy_id = config.strategy_id
        self._prev_state: dict[str, int] = {}  # symbol -> sign(fast - slow)
        self._fhash = features_hash(
            _FEATURES,
            {"sma": config.slow, "atr": config.atr_window},
        )

    # -- Strategy protocol --------------------------------------------- #
    def on_market_data(self, event: MarketData, ctx) -> list[Signal]:
        cfg = self.config
        if cfg.symbols and event.symbol not in cfg.symbols:
            return []

        bars = ctx.history(event.symbol, cfg.warmup + 5)
        if len(bars) < cfg.warmup:
            return []

        df = _to_frame(bars)
        fast = compute("sma", df, window=cfg.fast).iloc[-1]
        slow = compute("sma", df, window=cfg.slow).iloc[-1]
        atr = compute("atr", df, window=cfg.atr_window).iloc[-1]
        if pd.isna(fast) or pd.isna(slow) or pd.isna(atr) or atr <= 0:
            return []

        state = 1 if fast > slow else -1
        prev = self._prev_state.get(event.symbol, 0)
        self._prev_state[event.symbol] = state
        if state == prev or prev == 0:
            return []  # no fresh crossing

        price = event.price
        if state == 1:
            direction = Direction.LONG
            stop = price - cfg.stop_atr_mult * atr
            target = price + cfg.stop_atr_mult * atr * cfg.target_rr
        else:
            if not cfg.allow_short:
                # exit longs only
                if ctx.position(event.symbol) > 0:
                    return [self._signal(event, Direction.FLAT, price, None, None)]
                return []
            direction = Direction.SHORT
            stop = price + cfg.stop_atr_mult * atr
            target = price - cfg.stop_atr_mult * atr * cfg.target_rr

        return [self._signal(event, direction, price, stop, target)]

    def on_fill(self, event: Fill) -> None:  # notification only
        return None

    # -- helpers ---------------------------------------------------- #
    def _signal(
        self,
        event: MarketData,
        direction: Direction,
        entry: float,
        stop: float | None,
        target: float | None,
    ) -> Signal:
        return Signal(
            ts=event.ts,
            symbol=event.symbol,
            direction=direction,
            strategy_id=self.strategy_id,
            conviction=self.config.conviction,
            features_hash=self._fhash,
            entry_hint=entry,
            stop_hint=stop,
            target_hint=target,
            meta={"fast": self.config.fast, "slow": self.config.slow},
        )


def _to_frame(bars: list[MarketData]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": [b.ts for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close if b.close is not None else b.price for b in bars],
            "volume": [b.volume or 0.0 for b in bars],
        }
    )
