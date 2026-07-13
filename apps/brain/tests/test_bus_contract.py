"""El contrato del bus, desde el lado de brain.

El test que de verdad importa aquí es `test_los_tres_lados_declaran_los_mismos_campos`.
El contrato está escrito tres veces —JSON Schema, TypeScript, Python— porque ninguno
de los tres puede importar a los otros dos. Que no deriven no se consigue con
disciplina, se consigue con este test.

El resto son tests de lógica pura: entra un dict de strings, sale un mensaje
validado o un `ContractError`. Sin Redis, sin base de datos, sin reloj.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from umbra.bus.contract import (
    FILL_REQUIRED,
    INTENT_REQUIRED,
    ContractError,
    fields_from_entry,
    format_decimal,
    format_price,
    parse_fill,
    strategy_from_edge_name,
    validate_intent_fields,
)

_CONTRACTS = Path(__file__).resolve().parents[3] / "packages" / "contracts"
_INTENT_TS = Path(__file__).resolve().parents[3] / "apps" / "exec" / "src" / "bus" / "intent.ts"

_CONDITION_ID = "0x" + "ab" * 32
_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def _valid_intent_fields() -> dict[str, str]:
    return {
        "intent_id": _UUID,
        "ts": "2026-07-10T01:00:00+00:00",
        "strategy": "overreaction",
        "mode": "shadow",
        "condition_id": _CONDITION_ID,
        "token_id": "7100",
        "side": "BUY",
        "size_usd": "100.000000",
        "limit_price": "0.441000",
        "tif": "IOC",
        "max_slippage_bps": "500",
        "expires_at": "2026-07-10T01:01:00+00:00",
    }


def _valid_fill_fields() -> dict[str, str]:
    return {
        "intent_id": _UUID,
        "ts": "2026-07-10T01:00:01+00:00",
        "mode": "shadow",
        "status": "FILLED",
        "filled_shares": "238.095238",
        "avg_price": "0.420000",
        "notional_usd": "100.000000",
        "fees_usd": "0.000000",
        "order_id": "",
        "tx_hash": "",
        "mid_price": "0.415000",
        "expected_slippage_bps": "32",
        "realized_slippage_bps": "120",
        "error": "",
    }


# ---------------------------------------------------------------------------
# El invariante de los tres lados
# ---------------------------------------------------------------------------


def _ts_required(name: str) -> list[str]:
    """Extrae `export const NAME = [...] as const;` de `intent.ts`."""
    source = _INTENT_TS.read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = \[(.*?)\] as const;", source, re.S)
    assert match, f"no se encontró {name} en intent.ts"
    return re.findall(r"'([^']+)'", match.group(1))


@pytest.mark.parametrize(
    ("schema_file", "python_required", "ts_const"),
    [
        ("intent.schema.json", INTENT_REQUIRED, "INTENT_REQUIRED"),
        ("fill.schema.json", FILL_REQUIRED, "FILL_REQUIRED"),
    ],
)
def test_los_tres_lados_declaran_los_mismos_campos(schema_file, python_required, ts_const):
    """Si alguien añade un campo requerido al schema y se olvida de un lenguaje,
    el mensaje se rompe en producción. Que se rompa aquí."""
    schema = json.loads((_CONTRACTS / schema_file).read_text(encoding="utf-8"))
    assert list(python_required) == schema["required"]
    assert _ts_required(ts_const) == schema["required"]


def test_los_enums_del_schema_son_los_de_python():
    schema = json.loads((_CONTRACTS / "intent.schema.json").read_text(encoding="utf-8"))
    from umbra.bus.contract import BUS_SIDES, MODES, STRATEGIES, TIFS

    props = schema["properties"]
    assert list(STRATEGIES) == props["strategy"]["enum"]
    assert list(MODES) == props["mode"]["enum"]
    assert list(BUS_SIDES) == props["side"]["enum"]
    assert list(TIFS) == props["tif"]["enum"]

    fill_schema = json.loads((_CONTRACTS / "fill.schema.json").read_text(encoding="utf-8"))
    from umbra.bus.contract import FILL_STATUSES

    assert list(FILL_STATUSES) == fill_schema["properties"]["status"]["enum"]


# ---------------------------------------------------------------------------
# Formateo hacia el cable
# ---------------------------------------------------------------------------


def test_format_decimal_exige_seis_decimales_exactos():
    assert format_decimal(Decimal("100")) == "100.000000"
    assert format_decimal(Decimal("0.000001")) == "0.000001"


def test_format_decimal_rechaza_lo_que_no_esta_cuantizado():
    """Truncar en silencio aquí es el bug que la Fase 2 pasó una semana persiguiendo."""
    with pytest.raises(ContractError, match="más de 6 decimales"):
        format_decimal(Decimal("1.0000005"))


def test_format_decimal_rechaza_negativos():
    with pytest.raises(ContractError, match="negativos"):
        format_decimal(Decimal("-1.000000"))


def test_format_price_admite_los_extremos_y_rechaza_lo_de_fuera():
    assert format_price(Decimal("0")) == "0.000000"
    assert format_price(Decimal("1")) == "1.000000"
    with pytest.raises(ContractError, match=r"fuera de \[0, 1\]"):
        format_price(Decimal("1.000001"))


def test_strategy_from_edge_name_quita_la_version():
    assert strategy_from_edge_name("overreaction_v1") == "overreaction"
    assert strategy_from_edge_name("momentum_v1") == "momentum"
    assert strategy_from_edge_name("arb") == "arb"


def test_strategy_from_edge_name_no_inventa_estrategias():
    """Un edge desconocido no se cuela en el presupuesto de capital de otro."""
    with pytest.raises(ContractError, match="edge sin estrategia"):
        strategy_from_edge_name("liquidity_vacuum_v1")


# ---------------------------------------------------------------------------
# Validación del intent antes de publicarlo
# ---------------------------------------------------------------------------


def test_un_intent_bien_formado_valida():
    validate_intent_fields(_valid_intent_fields())


@pytest.mark.parametrize("campo", INTENT_REQUIRED)
def test_falta_un_campo_requerido(campo):
    fields = _valid_intent_fields()
    del fields[campo]
    with pytest.raises(ContractError, match="falta el campo requerido"):
        validate_intent_fields(fields)


@pytest.mark.parametrize("campo", INTENT_REQUIRED)
def test_un_campo_requerido_vacio_es_como_si_faltara(campo):
    fields = _valid_intent_fields()
    fields[campo] = ""
    with pytest.raises(ContractError, match="falta el campo requerido"):
        validate_intent_fields(fields)


@pytest.mark.parametrize(
    ("campo", "valor", "error"),
    [
        ("intent_id", "no-soy-un-uuid", "no es un uuid"),
        ("condition_id", "0xabc", "condition_id malformado"),
        ("strategy", "overreaction_v1", "strategy desconocida"),
        ("mode", "paper", "mode desconocido"),
        ("side", "BUY_YES", "side desconocido"),
        ("tif", "DAY", "tif desconocido"),
        ("size_usd", "-1.0", "size_usd no es un decimal"),
        ("limit_price", "1.5", r"limit_price fuera de \[0, 1\]"),
        ("limit_price", ".5", r"limit_price fuera de \[0, 1\]"),
        ("max_slippage_bps", "1001", r"fuera de \[0, 1000\]"),
        ("max_slippage_bps", "12.5", "no es un entero"),
        ("ts", "2026-07-10", "no es un instante ISO-8601"),
        ("expires_at", "mañana", "no es un instante ISO-8601"),
    ],
)
def test_campos_malformados(campo, valor, error):
    fields = _valid_intent_fields()
    fields[campo] = valor
    with pytest.raises(ContractError, match=error):
        validate_intent_fields(fields)


def test_el_modo_del_bus_no_conoce_sim_ni_paper():
    """`sim` y `paper` son modos de brain. El bus solo entiende `shadow` y `live`."""
    for mode in ("sim", "paper"):
        fields = _valid_intent_fields()
        fields["mode"] = mode
        with pytest.raises(ContractError, match="mode desconocido"):
            validate_intent_fields(fields)


# ---------------------------------------------------------------------------
# Lectura de fills
# ---------------------------------------------------------------------------


def test_parse_fill_lleno():
    fill = parse_fill(_valid_fill_fields())
    assert fill.intent_id == _UUID
    assert fill.status == "FILLED"
    assert fill.filled_shares == Decimal("238.095238")
    assert fill.notional_usd == Decimal("100.000000")
    assert fill.mid_price == Decimal("0.415000")
    assert fill.realized_slippage_bps == 120
    assert fill.expected_slippage_bps == 32
    assert not fill.is_empty


def test_parse_fill_rechazado_conserva_la_medicion():
    """El dato que la Fase 3 vino a buscar sobrevive al rechazo de la compuerta."""
    fields = _valid_fill_fields()
    fields.update(
        status="REJECTED",
        filled_shares="0.000000",
        avg_price="0.000000",
        notional_usd="0.000000",
        realized_slippage_bps="450",
        error="slippage 450bps > max_slippage_bps 300",
    )
    fill = parse_fill(fields)
    assert fill.is_empty
    assert fill.realized_slippage_bps == 450
    assert fill.error.startswith("slippage 450bps")


def test_parse_fill_sin_mid_ni_slippage():
    """Un libro de un solo lado no da mid. No se inventa un cero."""
    fields = _valid_fill_fields()
    del fields["mid_price"]
    del fields["realized_slippage_bps"]
    fill = parse_fill(fields)
    assert fill.mid_price is None
    assert fill.realized_slippage_bps is None


def test_parse_fill_admite_slippage_negativo():
    """Positivo = adverso. Un libro favorable da negativo, y es un número real."""
    fields = _valid_fill_fields()
    fields["realized_slippage_bps"] = "-45"
    assert parse_fill(fields).realized_slippage_bps == -45


def test_parse_fill_campos_opcionales_vacios_son_nulos():
    fields = _valid_fill_fields()
    fields["mid_price"] = ""
    fields["expected_slippage_bps"] = ""
    fill = parse_fill(fields)
    assert fill.mid_price is None
    assert fill.expected_slippage_bps is None


@pytest.mark.parametrize("campo", FILL_REQUIRED)
def test_parse_fill_exige_los_requeridos(campo):
    fields = _valid_fill_fields()
    del fields[campo]
    with pytest.raises(ContractError, match="falta el campo requerido"):
        parse_fill(fields)


@pytest.mark.parametrize(
    ("campo", "valor", "error"),
    [
        ("intent_id", "xxx", "no es un uuid"),
        ("mode", "paper", "mode desconocido"),
        ("status", "OK", "status desconocido"),
        ("filled_shares", "-1", "no es un decimal no negativo"),
        ("notional_usd", "cien", "no es un decimal no negativo"),
        ("mid_price", "1.5", r"mid_price fuera de \[0, 1\]"),
        ("realized_slippage_bps", "12.5", "no es un entero"),
    ],
)
def test_parse_fill_malformado(campo, valor, error):
    fields = _valid_fill_fields()
    fields[campo] = valor
    with pytest.raises(ContractError, match=error):
        parse_fill(fields)


def test_fields_from_entry_acepta_dict_y_lista_plana():
    assert fields_from_entry({"a": "1"}) == {"a": "1"}
    assert fields_from_entry(["a", "1", "b", "2"]) == {"a": "1", "b": "2"}
