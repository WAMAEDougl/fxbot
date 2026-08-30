from datetime import UTC, datetime

import pytest

from core.events import Direction, Order, OrderType, Side, Signal


def _utc():
    return datetime(2021, 6, 1, 14, 32, tzinfo=UTC)


def test_event_ts_must_be_utc():
    with pytest.raises(ValueError):
        Signal(ts=datetime(2021, 1, 1), symbol="X", direction=Direction.LONG, strategy_id="s")


def test_order_requires_bracket():
    with pytest.raises(ValueError, match="stop_loss and take_profit"):
        Order(
            ts=_utc(), client_order_id="c1", symbol="X", side=Side.BUY, qty=10,
            order_type=OrderType.MARKET,
        )


def test_order_bracket_sides_validated_for_buy():
    # BUY: need stop < entry < target
    with pytest.raises(ValueError, match="BUY bracket invalid"):
        Order(
            ts=_utc(), client_order_id="c1", symbol="X", side=Side.BUY, qty=10,
            order_type=OrderType.LIMIT, limit_price=100, stop_loss=101, take_profit=110,
        )
    Order(
        ts=_utc(), client_order_id="c1", symbol="X", side=Side.BUY, qty=10,
        order_type=OrderType.LIMIT, limit_price=100, stop_loss=98, take_profit=110,
    )


def test_signal_conviction_bounds():
    with pytest.raises(ValueError):
        Signal(ts=_utc(), symbol="X", direction=Direction.LONG, strategy_id="s", conviction=1.5)


def test_signal_is_not_an_order():
    sig = Signal(ts=_utc(), symbol="X", direction=Direction.LONG, strategy_id="s")
    assert not hasattr(sig, "qty")
    assert not hasattr(sig, "side")
