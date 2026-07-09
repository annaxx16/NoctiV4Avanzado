"""Test de integración: descarga real desde Gamma API.

Marcado como integration porque hace HTTP real. Si no hay red, salta.
"""

from __future__ import annotations

import pytest

from umbra.polymarket.client import GammaClient


@pytest.mark.asyncio
async def test_list_markets_returns_active_markets():
    async with GammaClient() as client:
        markets = await client.list_markets(active=True, closed=False, limit=5)

    assert len(markets) > 0, "Gamma debería devolver al menos un mercado activo"
    m = markets[0]
    assert m.condition_id, "condition_id no debe estar vacío"
    assert m.question, "question no debe estar vacío"
    assert m.active is True
    assert m.closed is False


@pytest.mark.asyncio
async def test_market_has_pricing_fields():
    async with GammaClient() as client:
        markets = await client.list_markets(
            active=True, closed=False, limit=10, order="volume24hr"
        )

    with_prices = [
        m for m in markets if m.best_bid is not None and m.best_ask is not None
    ]
    assert len(with_prices) > 0, "al menos algunos mercados activos deben tener bid/ask"
