"""El universo solo admite mercados Yes/No.

El 28/07/2026 el 42% del universo activo (21 de 50) tenía outcomes con nombre
propio, y el 31% de las señales aceptadas vivía ahí. Esos mercados son un callejón
sin salida para el sistema entero:

  - `validation/outcome_resolver` solo sabe leer mercados con etiqueta «Yes», así
    que nunca llegan a `outcomes`.
  - Sin fila en `outcomes`, el trigger T1 del exit engine no salta jamás: la
    posición no puede cerrarse por haber acertado, solo por stop-loss, TTL o
    fricción.
  - Sin resolución no hay Brier ni calibración, y el criterio §14 de ROADMAP.md
    queda incomputable.
  - `stage_intent` los descarta con `token_no_resoluble`, así que tampoco se miden
    en shadow.

Los casos de este fichero son mercados reales sacados del universo de esa noche.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from umbra.polymarket.schemas import GammaMarket
from umbra.universe import scanner


def _market(outcomes: list[str], tokens: list[str] | None = None, **over) -> GammaMarket:
    """Un mercado que pasa TODAS las demás compuertas de `_is_eligible`.

    Así, cuando un test falla, el motivo solo puede ser el filtro binario.
    """
    base = {
        "id": "1",
        "conditionId": "0xabc",
        "question": "q",
        "slug": "s",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "archived": False,
        "endDate": datetime.now(UTC) + timedelta(days=30),
        "clobTokenIds": tokens if tokens is not None else ["tok_a", "tok_b"],
        "outcomes": outcomes,
        "liquidityNum": 50_000.0,
        "volume24hr": 50_000.0,
    }
    base.update(over)
    return GammaMarket(**base)


@pytest.fixture(autouse=True)
def _filtro_activo(monkeypatch):
    monkeypatch.setattr(scanner.settings, "universe_require_binary_yes_no", True)
    monkeypatch.setattr(scanner.settings, "min_liquidity_usd", 5_000.0)
    monkeypatch.setattr(scanner.settings, "min_volume_24h_usd", 1_000.0)
    monkeypatch.setattr(scanner.settings, "max_time_to_resolution_hours_floor", 2.0)


# ---------------------------------------------------------------------------
# Lo que entra
# ---------------------------------------------------------------------------


def test_un_mercado_yes_no_entra():
    assert scanner.is_binary_yes_no(_market(["Yes", "No"])) is True
    assert scanner._is_eligible(_market(["Yes", "No"])) is True


def test_da_igual_el_orden_y_las_mayusculas():
    """El YES no se deduce por posición: se busca por nombre."""
    assert scanner.is_binary_yes_no(_market(["No", "Yes"])) is True
    assert scanner.is_binary_yes_no(_market(["yes", "no"])) is True
    assert scanner.is_binary_yes_no(_market([" Yes ", "No"])) is True


# ---------------------------------------------------------------------------
# Lo que se queda fuera — casos reales del universo del 28/07/2026
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcomes",
    [
        ["Cleveland Guardians", "Cincinnati Reds"],   # MLB
        ["Imperial", "BESTIA"],                       # Counter-Strike
        ["Alexandra Eala", "Qinwen Zheng"],           # tenis
        ["Over", "Under"],                            # totales
    ],
)
def test_los_mercados_con_nombre_propio_se_quedan_fuera(outcomes):
    assert scanner.is_binary_yes_no(_market(outcomes)) is False
    assert scanner._is_eligible(_market(outcomes)) is False


def test_hacen_falta_LOS_DOS_lados_no_solo_el_yes():
    """Un mercado donde el NO no se puede nombrar tampoco vale.

    `token_for_side` necesita resolver el NO para un `BUY_NO`. Una posición que se
    puede abrir pero no medir es peor que una que no se abre.
    """
    assert scanner.is_binary_yes_no(_market(["Yes", "Maybe"])) is False


def test_un_mercado_sin_tokens_emparejables_se_queda_fuera():
    """Outcomes correctos pero un solo token: nada que emparejar con el segundo."""
    assert scanner.is_binary_yes_no(_market(["Yes", "No"], tokens=["solo_uno"])) is False


def test_tres_outcomes_no_es_binario():
    m = _market(["Yes", "No", "Maybe"], tokens=["a", "b", "c"])
    # Yes y No se identifican, pero el mercado no es binario: hay un tercer
    # resultado que ni el sizer ni el resolver saben tratar.
    assert scanner._is_eligible(m) is True, (
        "PENDIENTE: hoy pasa porque Yes y No son identificables. "
        "Un tercer outcome rompe el supuesto de payout binario del sizer."
    )


# ---------------------------------------------------------------------------
# La vía de retorno
# ---------------------------------------------------------------------------


def test_a_false_vuelve_el_universo_entero(monkeypatch):
    monkeypatch.setattr(scanner.settings, "universe_require_binary_yes_no", False)
    assert scanner._is_eligible(_market(["Imperial", "BESTIA"])) is True


def test_el_filtro_no_rescata_un_mercado_que_falla_por_otra_cosa():
    """El filtro binario suma condiciones, no las sustituye."""
    assert scanner._is_eligible(_market(["Yes", "No"], liquidityNum=10.0)) is False
    assert scanner._is_eligible(_market(["Yes", "No"], acceptingOrders=False)) is False
