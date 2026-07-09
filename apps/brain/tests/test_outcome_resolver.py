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
