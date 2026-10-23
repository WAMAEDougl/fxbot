from datetime import UTC, datetime, timedelta

from core.events import MarketData
from execution.portfolio import Portfolio
from research.backtest import BacktestConfig, Backtester, InstrumentSpec
from risk.manager import RiskConfig

T0 = datetime(2024, 1, 2, tzinfo=UTC)


def _bar(sym, price):
    return MarketData(ts=T0, symbol=sym, source="t", open=price, high=price, low=price,
                      close=price, volume=1e6, bar_interval="1d")


def _bt():
    return Backtester(None, BacktestConfig(instruments={
        "AAA": InstrumentSpec(margin_rate=0.5), "BBB": InstrumentSpec(margin_rate=0.5)}))


def test_free_margin_is_equity_minus_margin_already_in_use():
    p = Portfolio(100_000.0, value_per_point={"AAA": 1.0, "BBB": 1.0}, history_len=10)
    for s in ("AAA", "BBB"):
        p.on_market_data(_bar(s, 100.0))
    bt = _bt()
    assert bt._free_margin(p) == 100_000.0  # nothing open
    p.seed_position("AAA", 1000, 100.0)  # 100k notional at 50% margin = 50k used
    assert abs(bt._free_margin(p) - 50_000.0) < 1e-6
    p.seed_position("BBB", 1000, 100.0)  # another 50k: account is now fully margined
    assert bt._free_margin(p) == 0.0  # never negative


def test_shorts_use_margin_too():
    p = Portfolio(100_000.0, value_per_point={"AAA": 1.0}, history_len=10)
    p.on_market_data(_bar("AAA", 100.0))
    p.seed_position("AAA", -600, 100.0)  # 60k short notional at 50% = 30k used
    assert abs(_bt()._free_margin(p) - 70_000.0) < 1e-6


def test_order_ttl_days_overrides_the_default_lifetime():
    assert RiskConfig().order_ttl == timedelta(days=1)
    assert RiskConfig(order_ttl_days=4).order_ttl == timedelta(days=4)
