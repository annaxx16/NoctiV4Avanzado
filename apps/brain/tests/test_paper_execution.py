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


# ---------------------------------------------------------------------------
# El medio spread — el término que faltaba
#
# La Fase 3 cotizó 315 fills contra el libro real y midió lo que este módulo
# predecía: 20 bps constantes frente a 191 realizados. Ajustando
# `k · medio_spread + size_factor · (nocional/liquidez)` por error absoluto
# mediano sobre esa muestra, el óptimo es k=1.0 y size_factor≈0; en el tramo de
# órdenes pequeñas (n=124) el residuo mediano contra el medio spread es 0.0.
# ---------------------------------------------------------------------------


def test_half_spread_is_measured_against_the_token_being_bought():
    """Los libros de YES y NO son espejo: mismo spread absoluto, distintos bps.

    Es la razón de que `_slippage_bps` reciba el precio teórico y no el mid del YES.
    Un céntimo de spread comprando NO a $0,05 son 1.000 bps; el mismo libro,
    comprando YES a $0,95, son 53.
    """
    from umbra.execution.paper import half_spread_bps

    caro = float(half_spread_bps(spread=0.01, price=0.05))
    barato = float(half_spread_bps(spread=0.01, price=0.95))
    assert caro == pytest.approx(1000.0, rel=1e-6)
    assert barato == pytest.approx(52.6316, rel=1e-4)


def test_half_spread_is_none_when_it_cannot_be_computed():
    """Sin spread no se devuelve cero: un cero diría que cruzar es gratis."""
    from umbra.execution.paper import half_spread_bps

    assert half_spread_bps(spread=None, price=0.5) is None
    assert half_spread_bps(spread=0.01, price=None) is None
    assert half_spread_bps(spread=0.01, price=0.0) is None
    assert half_spread_bps(spread=-0.01, price=0.5) is None


def test_spread_term_reproduces_the_measured_universe_median():
    """El caso típico medido: spread 0,0048 sobre un mid de $0,50 → ~48 bps.

    La divergencia mediana observada en la ventana de shadow fue +54 bps sobre un
    modelo que predecía 20. El término que faltaba explica el caso mediano entero.
    """
    bps = _slippage_bps(
        notional_usd=11.0, liquidity_usd=5_000.0, spread=0.0048, price=0.5
    )
    assert float(bps) == pytest.approx(48.0, abs=1.0)


def test_spread_dominates_the_size_term_at_realistic_sizes():
    """En la muestra real los ratios nocional/liquidez van de 1e-5 a 5e-4.

    A esos tamaños el término de impacto aporta centésimas de punto básico: es la
    razón de que el modelo anterior fuera, en producción, la constante `base`.
    """
    con_spread = _slippage_bps(
        notional_usd=11.0, liquidity_usd=5_000.0, spread=0.0048, price=0.5
    )
    sin_spread = _slippage_bps(notional_usd=11.0, liquidity_usd=5_000.0)
    assert con_spread > sin_spread * 2
    # Y el impacto, a este tamaño, es despreciable frente al spread.
    from umbra.config import settings

    impacto = settings.slippage_size_factor_bps * (11.0 / 5_000.0)
    assert impacto < 1.0


def test_base_bps_is_now_a_floor_not_an_addend():
    """Con spread cero sigue costando `slippage_base_bps` cruzar, y no menos."""
    from umbra.config import settings

    bps = _slippage_bps(notional_usd=10.0, liquidity_usd=1_000_000.0, spread=0.0, price=0.5)
    assert float(bps) == pytest.approx(settings.slippage_base_bps, abs=1e-6)


def test_spread_model_still_respects_the_cap():
    from umbra.config import settings

    # Un libro absurdo: 50 céntimos de spread sobre un contrato de 1 céntimo.
    bps = _slippage_bps(notional_usd=10.0, liquidity_usd=1_000.0, spread=0.50, price=0.01)
    assert float(bps) == pytest.approx(settings.slippage_cap_bps, abs=1e-6)


def test_without_spread_the_legacy_path_is_unchanged():
    """Compatibilidad: los llamantes que aún no propagan el libro no cambian.

    Este camino subestima el coste por un factor ~9. Existe para no romper nada,
    no porque sea defendible.
    """
    from umbra.config import settings

    bps = _slippage_bps(notional_usd=11.0, liquidity_usd=5_000.0)
    esperado = settings.slippage_base_bps + settings.slippage_size_factor_bps * (11.0 / 5_000.0)
    assert float(bps) == pytest.approx(esperado, abs=1e-4)


def test_fill_and_close_both_pay_the_spread_in_opposite_directions():
    """La ida y vuelta cuesta dos medios spreads. La Fase 3 solo midió el de ida."""
    from umbra.execution.paper import compute_close_price

    buy, bps_in = compute_fill_price(
        "BUY_YES", 0.5, notional_usd=50, liquidity_usd=10_000, spread=0.02
    )
    sell, bps_out = compute_close_price(
        "BUY_YES", 0.5, notional_usd=50, liquidity_usd=10_000, spread=0.02
    )
    assert bps_in == bps_out
    assert buy > 0.5 > sell
    # 0.02 de spread sobre 0.5 → 200 bps por cruce.
    assert float(bps_in) == pytest.approx(200.0, abs=1.0)
