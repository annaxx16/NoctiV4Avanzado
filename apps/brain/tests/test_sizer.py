"""Tests del Kelly fractional sizer."""

from __future__ import annotations

import pytest

from umbra.risk.sizer import _kelly_fraction, size_position


def test_kelly_zero_when_no_edge():
    # si p_fair == price, no hay edge
    assert _kelly_fraction(p_fair=0.5, price=0.5) == pytest.approx(0.0, abs=1e-9)


def test_kelly_positive_when_undervalued():
    # mercado dice 30%, nosotros pensamos 50% → comprar YES
    f = _kelly_fraction(p_fair=0.5, price=0.3)
    assert f > 0
    # f* = (0.5 * (0.7/0.3) - 0.5) / (0.7/0.3) = (0.5*2.333 - 0.5) / 2.333
    # = (1.166 - 0.5) / 2.333 = 0.286
    assert f == pytest.approx(0.286, abs=0.01)


def test_kelly_zero_when_overvalued_by_market():
    # nuestro p_fair < price → no apostar YES (Kelly clipea a 0)
    f = _kelly_fraction(p_fair=0.3, price=0.5)
    assert f == 0.0


def test_size_position_buy_yes_undervalued():
    res = size_position(
        side="BUY_YES",
        p_fair_yes=0.50,
        market_price_yes=0.30,
        bankroll=1000.0,
        kappa=0.15,
    )
    assert res.f_star > 0
    assert res.notional_usd > 0
    # notional = 0.15 * 1000 * f_star
    assert res.notional_usd == pytest.approx(0.15 * 1000 * res.f_star)


def test_size_position_buy_no_inverts_correctly():
    # mercado YES=0.70, p_fair_YES=0.50 → BUY_NO con precio_NO=0.30, p_fair_NO=0.50
    res = size_position(
        side="BUY_NO",
        p_fair_yes=0.50,
        market_price_yes=0.70,
        bankroll=1000.0,
        kappa=0.15,
    )
    assert res.f_star > 0
    assert res.notional_usd > 0
