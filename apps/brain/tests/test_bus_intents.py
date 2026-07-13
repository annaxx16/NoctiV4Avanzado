"""El productor de intents, en su parte pura: sin Redis y sin base de datos.

`stage_intent` y `publish_pending` tocan Postgres y viven en
`tests/test_bus_intents_db.py`. Aquí están las tres decisiones que se pueden
probar en una tabla: qué token compra cada lado, hasta dónde puede caminar el
libro exec, y qué sale exactamente por el cable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from umbra.bus.contract import ContractError, validate_intent_fields
from umbra.bus.intents import INTENT_TIF, intent_to_fields, limit_price_for
from umbra.bus.tokens import no_token_id, token_for_side, yes_token_id
from umbra.db.models import Intent

_CONDITION_ID = "0x" + "cd" * 32
_UUID = "11111111-2222-4333-8444-555555555555"


# ---------------------------------------------------------------------------
# Qué token compra cada lado
# ---------------------------------------------------------------------------


def test_el_yes_no_es_siempre_el_primer_token():
    """El orden de `outcomes` lo elige quien creó el mercado, no Polymarket."""
    assert yes_token_id(["No", "Yes"], ["tok_no", "tok_yes"]) == "tok_yes"
    assert no_token_id(["No", "Yes"], ["tok_no", "tok_yes"]) == "tok_no"


def test_outcomes_se_comparan_sin_mayusculas_ni_espacios():
    assert yes_token_id([" YES ", "no"], ["a", "b"]) == "a"


def test_comprar_no_es_comprar_el_token_no():
    """No es vender el token YES: son dos libros distintos."""
    outcomes, tokens = ["Yes", "No"], ["tok_yes", "tok_no"]
    assert token_for_side(outcomes, tokens, "BUY_YES") == "tok_yes"
    assert token_for_side(outcomes, tokens, "BUY_NO") == "tok_no"


def test_sin_outcome_identificable_no_hay_token():
    """Preferimos no medir a medir el token equivocado."""
    assert yes_token_id(["Trump", "Biden"], ["a", "b"]) is None
    assert no_token_id(["Trump", "Biden"], ["a", "b"]) is None
    assert token_for_side(["Trump", "Biden"], ["a", "b"], "BUY_YES") is None


def test_listas_descuadradas_o_vacias_no_revientan():
    assert yes_token_id(["Yes", "No"], ["solo_uno"]) == "solo_uno"
    assert no_token_id(["Yes", "No"], ["solo_uno"]) is None
    assert yes_token_id(None, None) is None
    assert yes_token_id([], []) is None


def test_un_side_desconocido_no_devuelve_token():
    assert token_for_side(["Yes", "No"], ["a", "b"], "SELL_YES") is None


# ---------------------------------------------------------------------------
# El límite: la tolerancia declarada, no la predicción
# ---------------------------------------------------------------------------


def test_el_limite_es_el_mid_mas_la_tolerancia():
    # 0.42 * 1.05 = 0.441
    assert limit_price_for("BUY_YES", Decimal("0.42"), 500) == Decimal("0.441000")


def test_comprar_no_parte_del_precio_del_token_no():
    # (1 - 0.42) * 1.05 = 0.609
    assert limit_price_for("BUY_NO", Decimal("0.42"), 500) == Decimal("0.609000")


def test_el_limite_no_se_sale_de_uno():
    """Un token caro con tolerancia ancha no puede pedir pagar más de $1."""
    assert limit_price_for("BUY_YES", Decimal("0.99"), 500) == Decimal("1")


def test_tolerancia_cero_es_el_mid_exacto():
    assert limit_price_for("BUY_YES", Decimal("0.42"), 0) == Decimal("0.420000")


def test_el_limite_redondea_a_la_baja_comprando():
    """Redondear al alza sería aceptar pagar un pelo más de lo declarado."""
    # 0.333333 * 1.0001 = 0.33336633... → hacia abajo, 0.333366
    assert limit_price_for("BUY_YES", Decimal("0.333333"), 1) == Decimal("0.333366")


def test_el_limite_siempre_es_un_precio_valido_para_el_contrato():
    for mid in ("0.001", "0.42", "0.5", "0.999"):
        for bps in (0, 1, 500, 1000):
            precio = limit_price_for("BUY_YES", Decimal(mid), bps)
            assert Decimal(0) <= precio <= Decimal(1)


# ---------------------------------------------------------------------------
# Qué sale por el cable
# ---------------------------------------------------------------------------


def _intent(**overrides) -> Intent:
    """Una fila de `intents` en memoria. No necesita sesión."""
    now = datetime(2026, 7, 10, 1, 0, 0, tzinfo=UTC)
    defaults = {
        "intent_id": _UUID,
        "ts": now,
        "signal_id": 42,
        "market_id": _CONDITION_ID,
        "strategy": "overreaction",
        "mode": "shadow",
        "token_id": "7100",
        "side": "BUY_YES",
        "action": "OPEN",
        "bus_side": "BUY",
        "size_usd": Decimal("100.000000"),
        "limit_price": Decimal("0.441000"),
        "tif": INTENT_TIF,
        "max_slippage_bps": 500,
        "expires_at": now + timedelta(seconds=60),
        "expected_slippage_bps": Decimal("32.4000"),
    }
    return Intent(**{**defaults, **overrides})


def test_los_campos_del_cable_validan_contra_el_contrato():
    validate_intent_fields(intent_to_fields(_intent()))


def test_el_cable_habla_de_tokens_no_de_posiciones():
    """`side` en el cable es BUY/SELL; `BUY_YES` se queda en la fila."""
    fields = intent_to_fields(_intent())
    assert fields["side"] == "BUY"
    assert fields["condition_id"] == _CONDITION_ID
    assert fields["token_id"] == "7100"


def test_el_ts_del_cable_es_el_de_la_fila():
    """Si divergen, `expires_at` mide desde un instante que no ocurrió."""
    intent = _intent()
    fields = intent_to_fields(intent)
    assert fields["ts"] == intent.ts.isoformat()
    assert fields["expires_at"] == intent.expires_at.isoformat()


def test_el_expected_slippage_viaja_como_entero_redondeado():
    """La columna guarda los 4 decimales; el cable, un entero. El reporte lee la columna."""
    assert intent_to_fields(_intent(expected_slippage_bps=Decimal("32.4000")))[
        "expected_slippage_bps"
    ] == "32"
    assert intent_to_fields(_intent(expected_slippage_bps=Decimal("32.5000")))[
        "expected_slippage_bps"
    ] == "33"


def test_los_opcionales_nulos_no_se_escriben():
    """Un `signal_id` vacío obligaría a exec a decidir qué significa. No tiene por qué."""
    fields = intent_to_fields(_intent(signal_id=None, expected_slippage_bps=None))
    assert "signal_id" not in fields
    assert "expected_slippage_bps" not in fields
    validate_intent_fields(fields)


def test_los_decimales_viajan_como_string_con_seis_decimales():
    fields = intent_to_fields(_intent())
    assert fields["size_usd"] == "100.000000"
    assert fields["limit_price"] == "0.441000"


def test_un_nocional_sin_cuantizar_no_llega_al_cable():
    """Truncar en silencio aquí desalinearía el fill de su propia fila."""
    with pytest.raises(ContractError, match="más de 6 decimales"):
        intent_to_fields(_intent(size_usd=Decimal("100.0000005")))


def test_el_tif_es_ioc():
    """`FOK` tiraría la muestra de los libros finos; `GTC` describe una orden que descansa."""
    assert INTENT_TIF == "IOC"
    assert intent_to_fields(_intent())["tif"] == "IOC"
