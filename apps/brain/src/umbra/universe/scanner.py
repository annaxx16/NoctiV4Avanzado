"""Universe scanner: descarga top mercados de Gamma, filtra por liquidez/volumen,
y upserta a la tabla `markets_active`.

Idempotente — corre en loop con scanner_loop().
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from umbra.cache.universe_cache import UniverseMarket, publish_universe
from umbra.config import settings
from umbra.db.models import Market, MarketActive
from umbra.db.session import get_sessionmaker
from umbra.logging import get_logger
from umbra.polymarket.client import GammaClient
from umbra.polymarket.schemas import GammaMarket

log = get_logger("umbra.universe")


def to_universe_markets(candidates: list[GammaMarket]) -> list[UniverseMarket]:
    """Traduce los candidatos a la forma que exec entiende.

    `_is_eligible` ya garantiza `condition_id` y `clob_token_ids` no vacíos, así
    que aquí no hay que defenderse de eso.
    """
    return [
        UniverseMarket(
            condition_id=m.condition_id,
            rank=rank,
            token_ids=list(m.clob_token_ids or []),
            liquidity_num=m.liquidity_num,
            volume_24hr=m.volume_24hr,
        )
        for rank, m in enumerate(candidates, start=1)
    ]


def _is_eligible(m: GammaMarket) -> bool:
    if not m.active or m.closed or m.archived or not m.accepting_orders:
        return False
    if m.end_date is not None:
        end_date = m.end_date if m.end_date.tzinfo else m.end_date.replace(tzinfo=UTC)
        min_end_date = datetime.now(UTC) + timedelta(
            hours=settings.max_time_to_resolution_hours_floor
        )
        if end_date <= min_end_date:
            return False
    if (m.liquidity_num or 0) < settings.min_liquidity_usd:
        return False
    if (m.volume_24hr or 0) < settings.min_volume_24h_usd:
        return False
    if not m.clob_token_ids or not m.condition_id:
        return False
    return True


async def _upsert_market(session, m: GammaMarket) -> None:
    stmt = (
        pg_insert(Market)
        .values(
            condition_id=m.condition_id,
            gamma_id=m.id,
            slug=m.slug,
            question=m.question,
            clob_token_ids=m.clob_token_ids,
            outcomes=m.outcomes,
            end_date=m.end_date,
            start_date=m.start_date,
            first_seen_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            index_elements=["condition_id"],
            set_={
                "slug": m.slug,
                "question": m.question,
                "clob_token_ids": m.clob_token_ids,
                "outcomes": m.outcomes,
                "end_date": m.end_date,
                "last_seen_at": datetime.now(UTC),
            },
        )
    )
    await session.execute(stmt)


async def scan_once() -> int:
    """Una pasada de scanning. Devuelve el nuevo tamaño del universo."""
    sm = get_sessionmaker()
    async with GammaClient(base_url=settings.polymarket_gamma_url) as client:
        candidates: list[GammaMarket] = []
        async for m in client.iter_markets(
            active=True, closed=False, order="volume24hr", page_size=100, max_pages=5
        ):
            if _is_eligible(m):
                candidates.append(m)
            if len(candidates) >= settings.universe_top_n:
                break

    log.info("universe.candidates", count=len(candidates))

    async with sm() as session:
        for m in candidates:
            await _upsert_market(session, m)
        await session.execute(delete(MarketActive))
        for rank, m in enumerate(candidates, start=1):
            session.add(
                MarketActive(
                    condition_id=m.condition_id,
                    rank=rank,
                    liquidity_num=Decimal(str(m.liquidity_num or 0)),
                    volume_24hr=Decimal(str(m.volume_24hr or 0)),
                    selected_at=datetime.now(UTC),
                )
            )
        await session.commit()

        result = await session.execute(select(MarketActive))
        size = len(result.scalars().all())

    # Publicar para exec. Postgres ya está commiteado: si Redis está caído, el
    # universo sigue siendo correcto y exec se quedará con el anterior hasta que
    # caduque. No es motivo para tirar el escaneo.
    try:
        await publish_universe(to_universe_markets(candidates))
    except Exception as exc:
        log.warning("universe.publish_failed", error=repr(exc), size=size)

    log.info("universe.updated", size=size)
    return size


async def scanner_loop(stop_event: asyncio.Event) -> None:
    """Loop infinito. Para detener: stop_event.set()."""
    while not stop_event.is_set():
        try:
            await scan_once()
        except Exception as exc:
            log.warning("universe.scan_failed", error=repr(exc))
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.universe_scan_interval_sec
            )
        except TimeoutError:
            continue
