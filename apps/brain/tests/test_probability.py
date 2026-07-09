"""Tests puros del probability engine (passthrough v1 con clamp)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from umbra.edges.overreaction import EdgeOutput
from umbra.engine.probability import compute_p_fair


def _edge(fair: float) -> EdgeOutput:
    return EdgeOutput(
        edge_name="overreaction_v1",
        side="BUY_YES",
        market_price=0.5,
        fair_price=fair,
        edge_value=0.1,
        strength=3.0,
        reason="test",
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_passthrough_devuelve_fair_en_rango():
    assert compute_p_fair(_edge(0.42)) == pytest.approx(0.42)


def test_clamp_extremo_superior():
    # Una EMA degenerada en 1.0 no debe darle a Kelly una certeza absoluta.
    assert compute_p_fair(_edge(1.0)) == pytest.approx(0.999)


def test_clamp_extremo_inferior():
    assert compute_p_fair(_edge(0.0)) == pytest.approx(0.001)
