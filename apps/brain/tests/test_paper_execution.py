"""Tests del Paper Execution Engine."""

from __future__ import annotations

import pytest

from umbra.execution.paper import _slippage_bps, compute_fill_price


def test_slippage_base_with_unknown_liquidity():
    bps = _slippage_bps(notional_usd=100.0, liquidity_usd=None)
    # con liquidez desconocida: aplicamos slippage cap o casi-cap
    assert bps > 0


def test_slippage_grows_with_size_relative_to_liquidity():
    small = _slippage_bps(notional_usd=10.0, liquidity_usd=10_000.0)
    big = _slippage_bps(notional_usd=1_000.0, liquidity_usd=10_000.0)
    assert big > small


def test_slippage_caps():
    enormous = _slippage_bps(notional_usd=1_000_000.0, liquidity_usd=10.0)
    from umbra.config import settings

    assert enormous <= settings.slippage_cap_bps + 1e-6


def test_buy_yes_fill_price_above_mid():
    fill, bps = compute_fill_price("BUY_YES", mid_yes=0.40, notional_usd=50, liquidity_usd=10_000)
    assert fill > 0.40
    assert bps > 0


def test_buy_no_fill_price_above_implicit_no_price():
    # mid_yes=0.40 → mid_no=0.60. BUY_NO debe pagar más de 0.60.
    fill, _ = compute_fill_price("BUY_NO", mid_yes=0.40, notional_usd=50, liquidity_usd=10_000)
    assert fill > 0.60
    assert fill < 1.0


def test_invalid_side_raises():
    with pytest.raises(ValueError):
        compute_fill_price("HODL", mid_yes=0.5, notional_usd=10, liquidity_usd=1000)
