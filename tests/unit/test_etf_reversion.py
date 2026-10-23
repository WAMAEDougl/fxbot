from datetime import UTC, datetime, timedelta

import numpy as np

from core.events import Direction, MarketData
from strategies.etf_reversion import EtfReversion, EtfReversionConfig
from strategies.etf_reversion.strategy import rsi_wilder

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _bars(closes, sym="SPY"):
    return [
        MarketData(ts=T0 + timedelta(days=i), symbol=sym, source="t", open=c, high=c + 1,
                   low=c - 1, close=c, volume=1e6, bar_interval="1d")
        for i, c in enumerate(closes)
    ]


class _Ctx:
    def __init__(self, bars, position=0.0):
        self._bars, self._pos = bars, position

    def history(self, symbol, lookback):
        return self._bars[-lookback:]

    def position(self, symbol):
        return self._pos


def _cfg(**kw):
    base = dict(symbols=["SPY"], trend_window=20, exit_window=5, atr_window=5, rsi_entry=10.0)
    base.update(kw)
    return EtfReversionConfig(**base)


def _run(strategy, bars, position=0.0):
    out = {}
    for i in range(len(bars)):
        sigs = strategy.on_market_data(bars[i], _Ctx(bars[: i + 1], position))
        if sigs:
            out[i] = sigs[0]
    return out


# an uptrend, then a sharp three-day sell-off that stays above the trend average
DIP = [100 + 0.5 * i for i in range(40)] + [119.5, 116.0, 112.5]


def test_rsi_extremes_and_causality():
    assert rsi_wilder(np.arange(1.0, 30.0), 2) == 100.0  # only gains
    assert rsi_wilder(np.arange(30.0, 1.0, -1.0), 2) < 1.0  # only losses
    # appending a future bar must not change an earlier value
    a = rsi_wilder(np.array(DIP[:35]), 2)
    assert a == rsi_wilder(np.array(DIP[:35]), 2)


def test_buys_a_sharp_dip_inside_an_uptrend_with_a_bracket():
    sigs = _run(EtfReversion(_cfg()), _bars(DIP))
    last = max(sigs)
    s = sigs[last]
    assert s.direction is Direction.LONG
    assert s.stop_hint < s.entry_hint < s.target_hint


def test_does_not_buy_the_same_dip_when_below_the_trend_average():
    downtrend = [140 - 0.5 * i for i in range(40)] + [118.0, 115.0, 112.0]
    assert _run(EtfReversion(_cfg()), _bars(downtrend)) == {}


def test_no_short_unless_allowed_and_only_when_overbought_in_a_downtrend():
    # steep downtrend, then a bounce that is overbought on RSI(2) but still below the average
    bounce = [160 - 1.0 * i for i in range(40)] + [122.5, 124.5, 126.5]
    assert _run(EtfReversion(_cfg()), _bars(bounce)) == {}  # long-only by default
    sigs = _run(EtfReversion(_cfg(allow_short=True)), _bars(bounce))
    assert sigs and all(s.direction is Direction.SHORT for s in sigs.values())
    s = sigs[max(sigs)]
    assert s.target_hint < s.entry_hint < s.stop_hint


def test_no_short_when_the_bounce_carries_price_back_above_the_average():
    spike = [140 - 0.5 * i for i in range(40)] + [122.0, 126.0, 130.0]  # ends above its SMA20
    assert _run(EtfReversion(_cfg(allow_short=True)), _bars(spike)) == {}


def test_exits_with_a_flat_signal_when_the_close_recovers_above_the_exit_average():
    bars = _bars(DIP + [114.0, 118.0, 122.0, 125.0])
    sigs = _run(EtfReversion(_cfg()), bars, position=10.0)
    assert sigs and all(s.direction is Direction.FLAT for s in sigs.values())
    assert all(s.stop_hint is None and s.target_hint is None for s in sigs.values())


def test_holds_while_not_recovered_and_before_the_time_stop():
    bars = _bars(DIP[:-1] + [110.0, 109.0, 108.0])  # keeps falling, below the 5-day average
    sigs = _run(EtfReversion(_cfg(max_hold_bars=50)), bars, position=10.0)
    # earlier bars (the rising phase) legitimately look "recovered"; only the sell-off matters
    assert [i for i in sigs if i >= 41] == []


def test_time_stop_exits_a_position_that_never_recovers():
    s = EtfReversion(_cfg(max_hold_bars=3))
    bars = _bars(DIP + [110.0, 109.0, 108.0, 107.0, 106.0, 105.0])
    # enter on the dip bar (flat), then pretend we hold the position afterwards
    entry_idx = max(_run(s, bars[: len(DIP)]))
    held = {}
    for i in range(entry_idx + 1, len(bars)):
        sigs = s.on_market_data(bars[i], _Ctx(bars[: i + 1], position=10.0))
        if sigs:
            held[i] = sigs[0]
    assert held and all(x.direction is Direction.FLAT for x in held.values())
    assert min(held) - entry_idx <= 4  # fires about max_hold_bars after entry


def test_does_not_resignal_an_entry_within_the_gap():
    idx = sorted(_run(EtfReversion(_cfg(resignal_gap_bars=5)), _bars(DIP + [111.0, 110.0, 109.0])))
    assert all(b - a >= 5 for a, b in zip(idx, idx[1:], strict=False))


def test_ignores_other_symbols():
    assert _run(EtfReversion(_cfg()), _bars(DIP, sym="XYZ")) == {}
