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

from umbra.bus.tokens import no_token_id as bus_no_token_id
from umbra.bus.tokens import yes_token_id as bus_yes_token_id
from umbra.cache.universe_cache import UniverseMarket, publish_universe
from umbra.config import settings
from umbra.db.models import Market, MarketActive
from umbra.db.session import get_sessionmaker
from umbra.logging import get_logger
from umbra.polymarket.client import GammaClient
from umbra.polymarket.schemas import GammaMarket

log = get_logger("umbra.universe")


def yes_token_id(m: GammaMarket) -> str | None:
    """El token del outcome YES, emparejando `outcomes` con `clob_token_ids`.

    Devuelve None si no hay un YES identificable: es preferible que exec no
    publique ese mercado a que publique el libro del lado equivocado.

    La regla vive en `bus/tokens.py` porque el productor de intents necesita la
    misma, y en el camino del dinero. Aquí solo se desenvuelve el `GammaMarket`.
    """
    return bus_yes_token_id(m.outcomes, m.clob_token_ids)


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
            yes_token_id=yes_token_id(m),
            liquidity_num=m.liquidity_num,
            volume_24hr=m.volume_24hr,
        )
        for rank, m in enumerate(candidates, start=1)
    ]


def is_binary_yes_no(m: GammaMarket) -> bool:
    """¿Es un mercado Yes/No con los dos tokens identificables?

    No se comprueba mirando `outcomes` por nuestra cuenta: se le pregunta a
    `bus/tokens.py`, que es donde vive la regla y que ya devuelve `None` cuando el
    outcome no se llama literalmente «Yes» o «No». Escribirla aquí otra vez sería
    tenerla mal en uno de los dos sitios el día que cambie.

    Exigir AMBOS lados, y no solo el YES, es deliberado: `token_for_side` necesita
    resolver el NO para un `BUY_NO`, y una posición que se puede abrir pero no
    medir es peor que una que no se abre.
    """
    return (
        bus_yes_token_id(m.outcomes, m.clob_token_ids) is not None
        and bus_no_token_id(m.outcomes, m.clob_token_ids) is not None
    )


def _is_eligible(m: GammaMarket) -> bool:
    if not m.active or m.closed or m.archived or not m.accepting_orders:
        return False
    # Mercados que el sistema no sabe resolver.
    #
    # El 42% del universo activo del 28/07/2026 tenía outcomes con nombre propio
    # —["Cleveland Guardians", "Cincinnati Reds"], ["Imperial", "BESTIA"],
    # ["Over", "Under"]— y el 31% de las señales aceptadas vivía ahí.
    #
    # `validation/outcome_resolver.resolve_yes_outcome` solo sabe leer mercados con
    # etiqueta «Yes», así que esos NUNCA llegan a `outcomes`. Sin fila en `outcomes`
    # el trigger T1 del exit engine no salta jamás: la posición no puede salir por
    # acertar, solo por stop-loss, TTL o fricción. Y sin resolución no hay Brier, no
    # hay calibración y el criterio §14 de ROADMAP.md es incomputable.
    #
    # Además la premisa del edge no traslada. En un mercado Yes/No sobre un evento
    # un salto de precio puede ser pánico; en «Guardians vs Reds» suele ser
    # información —cambia el pitcher, hay una lesión— y no hay media a la que
    # revertir.
    if settings.universe_require_binary_yes_no and not is_binary_yes_no(m):
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
