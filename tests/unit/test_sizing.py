import pytest

from risk.sizing import RISK_FRACTION_HARD_CAP, SizingParams, realised_risk, size_from_risk


def test_size_matches_risk_budget():
    r = size_from_risk(
        equity=100_000, entry=100.0, stop=98.0, risk_fraction=0.005,
        params=SizingParams(value_per_point=1.0, lot_step=1.0, min_lot=1.0),
    )
    # risk_amount = 500; stop_distance = 2 -> 250 units
    assert r.size == 250
    assert realised_risk(r.size, 100.0, 98.0, 1.0) == pytest.approx(500.0)


def test_hard_cap_enforced():
    r = size_from_risk(
        equity=100_000, entry=100.0, stop=99.0, risk_fraction=0.10,  # absurd
        params=SizingParams(value_per_point=1.0, lot_step=1.0),
    )
    assert r.capped
    assert r.risk_fraction_used <= RISK_FRACTION_HARD_CAP + 1e-12


def test_conviction_scales_down_only():
    base = size_from_risk(
        equity=100_000, entry=100, stop=98, risk_fraction=0.01,
        params=SizingParams(), conviction=1.0,
    )
    half = size_from_risk(
        equity=100_000, entry=100, stop=98, risk_fraction=0.01,
        params=SizingParams(), conviction=0.5,
    )
    assert half.size == pytest.approx(base.size / 2, rel=0.01)


def test_lot_step_floors():
    r = size_from_risk(
        equity=10_000, entry=100, stop=99, risk_fraction=0.01,
        params=SizingParams(value_per_point=1.0, lot_step=10.0, min_lot=10.0),
    )
    assert r.size % 10 == 0


def test_subminimum_size_returns_zero():
    r = size_from_risk(
        equity=100, entry=100, stop=50, risk_fraction=0.005,
        params=SizingParams(value_per_point=1.0, lot_step=1.0, min_lot=1.0),
    )
    assert r.size == 0.0


def test_zero_stop_distance_raises():
    with pytest.raises(ValueError):
        size_from_risk(equity=1000, entry=100, stop=100, risk_fraction=0.01, params=SizingParams())
