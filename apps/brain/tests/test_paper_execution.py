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


# ---------------------------------------------------------------------------
# Decimal, no float
# ---------------------------------------------------------------------------


def test_prices_and_bps_are_decimal_quantized_to_their_column_scale():
    from decimal import Decimal

    from umbra.execution.paper import BPS, PRICE

    fill, bps = compute_fill_price("BUY_YES", mid_yes=0.4, notional_usd=50, liquidity_usd=10_000)
    assert isinstance(fill, Decimal)
    assert isinstance(bps, Decimal)
    # `Numeric(12,6)` y `Numeric(10,4)`: si Postgres va a redondear, redondeamos antes.
    assert fill == fill.quantize(PRICE)
    assert bps == bps.quantize(BPS)


def test_float_inputs_never_leak_binary_error_into_the_price():
    """`Decimal(0.1)` no es `Decimal("0.1")`. La frontera va por `str`."""
    from decimal import Decimal

    from umbra.config import settings
    from umbra.execution.paper import compute_close_price

    # Sin slippage, el precio de cierre debe ser exactamente el mid.
    old = settings.slippage_base_bps, settings.slippage_size_factor_bps
    settings.slippage_base_bps, settings.slippage_size_factor_bps = 0.0, 0.0
    try:
        price, bps = compute_close_price("BUY_YES", 0.1, notional_usd=10, liquidity_usd=1000)
        assert bps == Decimal("0")
        assert price == Decimal("0.100000")
        # Y el lado NO: 1 - 0.1, exacto. En float sería 0.8999999999999999.
        price_no, _ = compute_close_price("BUY_NO", 0.1, notional_usd=10, liquidity_usd=1000)
        assert price_no == Decimal("0.900000")
    finally:
        settings.slippage_base_bps, settings.slippage_size_factor_bps = old


def test_slippage_is_adverse_on_both_sides():
    buy, _ = compute_fill_price("BUY_YES", 0.5, notional_usd=100, liquidity_usd=1000)
    from umbra.execution.paper import compute_close_price

    sell, _ = compute_close_price("BUY_YES", 0.5, notional_usd=100, liquidity_usd=1000)
    assert buy > sell, "comprar caro y vender barato: nunca al revés"
