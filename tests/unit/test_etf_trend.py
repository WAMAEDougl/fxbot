from datetime import UTC, datetime, timedelta

from core.events import Direction, MarketData
from strategies.etf_trend import EtfTrend, EtfTrendConfig

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
    return EtfTrendConfig(symbols=["SPY"], lookback=20, atr_window=5, **kw)


def _last_signals(strategy, bars, position=0.0):
    out = []
    for i in range(len(bars)):
        out = strategy.on_market_data(bars[i], _Ctx(bars[: i + 1], position))
    return out


def test_long_above_sma_with_bracket_geometry():
    bars = _bars([100 + i for i in range(40)])  # steady uptrend
    (sig,) = _last_signals(EtfTrend(_cfg(stop_atr_mult=3.0, target_rr=3.0)), bars)
    assert sig.direction is Direction.LONG
    assert sig.stop_hint < sig.entry_hint < sig.target_hint
    reward_to_risk = (sig.target_hint - sig.entry_hint) / (sig.entry_hint - sig.stop_hint)
    assert abs(reward_to_risk - 3.0) < 1e-9  # target_rr


def test_short_below_sma_only_when_allowed():
    bars = _bars([200 - i for i in range(40)])
    (sig,) = _last_signals(EtfTrend(_cfg()), bars)
    assert sig.direction is Direction.SHORT
    assert sig.target_hint < sig.entry_hint < sig.stop_hint
    assert _last_signals(EtfTrend(_cfg(allow_short=False)), bars) == []


def test_no_signal_while_in_position_or_during_warmup():
    bars = _bars([100 + i for i in range(40)])
    assert _last_signals(EtfTrend(_cfg()), bars, position=10.0) == []
    assert _last_signals(EtfTrend(_cfg()), bars[:10]) == []


def test_does_not_resignal_within_gap_bars():
    s = EtfTrend(_cfg(resignal_gap_bars=3))
    bars = _bars([100 + i for i in range(40)])
    fired = [i for i in range(len(bars)) if s.on_market_data(bars[i], _Ctx(bars[: i + 1]))]
    assert all(b - a >= 3 for a, b in zip(fired, fired[1:], strict=False))
    assert len(fired) >= 2  # it does re-signal once the gap has passed


def test_ignores_symbols_outside_config():
    bars = _bars([100 + i for i in range(40)], sym="XYZ")
    assert _last_signals(EtfTrend(_cfg()), bars) == []


# ------------------------------------------------------------ exit on flip #
def _reversal(n_up=30, n_down=15):
    return _bars([100 + i for i in range(n_up)] + [100 + n_up - 1 - 3 * i for i in range(1, n_down)])


def _signals_by_bar(strategy, bars, position):
    out = {}
    for i in range(len(bars)):
        sigs = strategy.on_market_data(bars[i], _Ctx(bars[: i + 1], position))
        if sigs:
            out[i] = sigs[0]
    return out


def test_exits_with_a_flat_signal_when_trend_turns_against_a_long():
    sigs = _signals_by_bar(EtfTrend(_cfg(exit_on_trend_flip=True)), _reversal(), position=25.0)
    assert sigs, "expected an exit signal after the trend flipped"
    assert all(s.direction is Direction.FLAT for s in sigs.values())
    assert all(s.stop_hint is None and s.target_hint is None for s in sigs.values())


def test_no_exit_signal_while_the_trend_still_agrees_with_the_position():
    bars = _bars([100 + i for i in range(40)])  # still rising
    assert _last_signals(EtfTrend(_cfg(exit_on_trend_flip=True)), bars, position=25.0) == []


def test_no_exit_signal_unless_the_option_is_on():
    assert _signals_by_bar(EtfTrend(_cfg()), _reversal(), position=25.0) == {}


def test_exit_signal_is_not_repeated_within_the_gap():
    sigs = _signals_by_bar(
        EtfTrend(_cfg(exit_on_trend_flip=True, resignal_gap_bars=5)), _reversal(), position=25.0)
    idx = sorted(sigs)
    assert all(b - a >= 5 for a, b in zip(idx, idx[1:], strict=False))


def test_short_position_exits_when_price_moves_back_above_the_average():
    bars = _bars([200 - i for i in range(30)] + [171 + 4 * i for i in range(1, 15)])
    sigs = _signals_by_bar(EtfTrend(_cfg(exit_on_trend_flip=True)), bars, position=-25.0)
    assert sigs and all(s.direction is Direction.FLAT for s in sigs.values())
