"""Resolución de outcomes (GAP-02).

Parte pura: `resolve_yes_outcome` deduce de un GammaMarket si YES resolvió 1 o 0.
Parte con I/O: `resolve_pending_outcomes` busca mercados ya vencidos sin fila en
`outcomes`, los consulta en Gamma y persiste la resolución. El cierre de las
posiciones contra el outcome lo hace el exit engine (trigger T1) en su próximo
tick — aquí solo registramos el hecho.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.config import settings
from umbra.db.models import Market, Outcome
from umbra.logging import get_logger
from umbra.polymarket.client import GammaClient
from umbra.polymarket.schemas import GammaMarket

log = get_logger("umbra.outcomes")

# Tolerancia para considerar un precio de outcome como resuelto (1 o 0).
_RESOLVED_HI = 0.99
_RESOLVED_LO = 0.01


def _to_float(v: str | float | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def resolve_yes_outcome(market: GammaMarket) -> bool | None:
    """Devuelve True si YES resolvió 1, False si 0, None si aún no es concluyente.

    Solo aplica a mercados binarios con etiqueta "Yes". Un mercado `closed` pero
    con precios fraccionarios (p.ej. anulado/50-50) devuelve None: no inventamos
    una resolución que no existe.
    """
    if not market.closed:
        return None
    labels = [o.strip().lower() for o in market.outcomes]
    prices = [_to_float(p) for p in market.outcome_prices]
    if not labels or len(labels) != len(prices) or "yes" not in labels:
        return None
    yes_price = prices[labels.index("yes")]
    if yes_price is None:
        return None
    if yes_price >= _RESOLVED_HI:
        return True
    if yes_price <= _RESOLVED_LO:
        return False
    return None


async def _markets_pending_resolution(
    session: AsyncSession, now: datetime, limit: int
) -> list[str]:
    resolved = select(Outcome.market_id)
    stmt = (
        select(Market.condition_id)
        .where(
            Market.end_date.is_not(None),
            Market.end_date < now,
            Market.condition_id.notin_(resolved),
        )
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def resolve_pending_outcomes(
    session: AsyncSession, *, limit: int = 100
) -> int:
    """Resuelve hasta `limit` mercados vencidos sin outcome. Devuelve cuántos
    se registraron en esta pasada."""
    now = datetime.now(UTC)
    pending = await _markets_pending_resolution(session, now, limit)
    if not pending:
        return 0

    resolved_count = 0
    async with GammaClient(base_url=settings.polymarket_gamma_url) as client:
        try:
            markets = await client.get_markets_by_condition_ids(pending)
        except Exception as exc:
            log.warning(
                "outcomes.gamma_unavailable",
                error=repr(exc),
                pending=len(pending),
            )
            return 0

    for cid in pending:
        market = markets.get(cid)
        if market is None:
            continue
        yes = resolve_yes_outcome(market)
        if yes is None:
            continue
        session.add(
            Outcome(
                market_id=cid,
                resolved_at=now,
                yes_outcome=yes,
                source="gamma_api",
            )
        )
        resolved_count += 1
        log.info("outcome.resolved", condition_id=cid, yes_outcome=yes)

    return resolved_count
