"""Tests de la parte pura del resolver de outcomes."""

from __future__ import annotations

from umbra.polymarket.schemas import GammaMarket
from umbra.validation.outcome_resolver import resolve_yes_outcome


def _market(closed: bool, outcomes, prices) -> GammaMarket:
    return GammaMarket.model_validate(
        {
            "id": "1",
            "conditionId": "0xabc",
            "question": "q",
            "slug": "s",
            "closed": closed,
            "outcomes": outcomes,
            "outcomePrices": prices,
        }
    )


def test_yes_gana():
    assert resolve_yes_outcome(_market(True, ["Yes", "No"], ["1", "0"])) is True


def test_no_gana():
    assert resolve_yes_outcome(_market(True, ["Yes", "No"], ["0", "1"])) is False


def test_no_resuelto_si_no_closed():
    assert resolve_yes_outcome(_market(False, ["Yes", "No"], ["1", "0"])) is None


def test_no_concluyente_si_precios_fraccionarios():
    # closed pero 50/50 (p.ej. anulado) → no inventamos resolución
    assert resolve_yes_outcome(_market(True, ["Yes", "No"], ["0.5", "0.5"])) is None


def test_mercado_no_binario_se_ignora():
    m = _market(True, ["Trump", "Biden", "Other"], ["1", "0", "0"])
    assert resolve_yes_outcome(m) is None


# ---------------------------------------------------------------------------
# El bug que dejó `outcomes` vacía durante semanas
# ---------------------------------------------------------------------------


async def test_el_cliente_pide_closed_explicitamente():
    """`/markets` de Gamma filtra a NO cerrados por defecto, aun preguntando por
    `condition_id` exacto. Medido contra la API real:

        sin el parámetro  ->  1 de 20 devueltos
        closed=true       -> 19 de 20 devueltos

    Un mercado resuelto es siempre un mercado cerrado. Sin pedirlo, el resolver
    preguntaba por vencidos y no recibía casi nada: `outcomes` llevaba semanas a
    cero, y con ella el Brier, la calibración y GAP-01.
    """
    from umbra.polymarket.client import GammaClient

    capturado: dict = {}

    class _Fake(GammaClient):
        async def _get(self, path, params=None):
            capturado.update(params or {})
            return []

    async with _Fake(base_url="http://x") as c:
        await c.get_markets_by_condition_ids(["0xa"], closed=True)
    assert capturado.get("closed") == "true"

    capturado.clear()
    async with _Fake(base_url="http://x") as c:
        await c.get_markets_by_condition_ids(["0xa"])
    assert "closed" not in capturado, "sin pedirlo, el poller sigue viendo mercados vivos"
