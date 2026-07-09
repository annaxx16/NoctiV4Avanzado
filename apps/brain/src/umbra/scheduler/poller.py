"""Poller: cada N segundos, para cada mercado del universo persiste un snapshot
en book_snapshots y evalúa la señal.

Dos fuentes, y no se sustituyen: se complementan.

- **Gamma (REST, este loop)**: una sola petición en batch para todo el universo.
  Es la única fuente de `active` / `accepting_orders`, y el respaldo cuando exec
  no está publicando. Detecta en 30s que un mercado dejó de aceptar órdenes.
- **WebSocket (exec → `book:{condition_id}`)**: precio fresco (~1s en vez de 30s)
  y, sobre todo, **profundidad real del libro**. Gamma no da `bids`/`asks`.

Cuando hay un book de WS fresco, sus precios pisan a los de Gamma en el snapshot.
Los flags de estado siguen viniendo de Gamma siempre. Si exec no publica, esto se
comporta exactamente como antes de la Fase 1.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from umbra.cache.book_cache import (
    SOURCE_CLOB_WS,
    CachedBook,
    age_seconds,
    get_book,
    now_iso,
    set_book,
)
from umbra.config import settings
from umbra.db.models import BookSnapshot, MarketActive
from umbra.db.session import get_sessionmaker
from umbra.engine.orchestrator import evaluate_market
from umbra.logging import get_logger
from umbra.polymarket.client import GammaClient
from umbra.polymarket.schemas import GammaMarket

log = get_logger("umbra.poller")


def _dec(v: float | None) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def is_usable_ws_book(book: CachedBook | None, *, now: datetime | None = None) -> bool:
    """¿Podemos fiarnos de este book del WebSocket para poner precio a un snapshot?

    Tres condiciones. Que lo haya escrito exec (no el propio poller, o estaríamos
    leyendo nuestra propia salida), que sea reciente, y que tenga un precio dentro
    del cual haya mercado. Un book sin bid ni ask no vale para nada.
    """
    if book is None or book.source != SOURCE_CLOB_WS:
        return False
    if age_seconds(book, now=now) > settings.ws_book_max_age_sec:
        return False
    return book.best_bid is not None or book.best_ask is not None


def build_snapshot(
    condition_id: str,
    market: GammaMarket,
    ws_book: CachedBook | None = None,
    *,
    now: datetime | None = None,
) -> BookSnapshot:
    """Compone el snapshot mezclando ambas fuentes.

    Precios del WebSocket si los hay; estado y volumen siempre de Gamma. Función
    pura: no toca Redis ni Postgres.
    """
    use_ws = is_usable_ws_book(ws_book, now=now)

    if use_ws:
        assert ws_book is not None  # is_usable_ws_book ya lo garantiza
        best_bid = ws_book.best_bid
        best_ask = ws_book.best_ask
        spread = ws_book.spread
        # El WS solo conoce el último trade si ha visto uno desde que conectó.
        # Si aún no, el de Gamma es mejor que ninguno.
        last_trade_price = (
            ws_book.last_trade_price
            if ws_book.last_trade_price is not None
            else market.last_trade_price
        )
    else:
        best_bid = market.best_bid
        best_ask = market.best_ask
        spread = market.spread
        last_trade_price = market.last_trade_price

    return BookSnapshot(
        market_id=condition_id,
        ts=now or datetime.now(UTC),
        best_bid=_dec(best_bid),
        best_ask=_dec(best_ask),
        last_trade_price=_dec(last_trade_price),
        spread=_dec(spread),
        # Liquidez y volumen no los da el WebSocket. Gamma es la única fuente.
        liquidity_num=_dec(market.liquidity_num),
        volume_24hr=_dec(market.volume_24hr),
        # `active` y `accepting_orders` son NOT NULL, y solo Gamma los conoce.
        # Por eso este loop no se puede apagar aunque exec esté publicando.
        active=market.active,
        accepting_orders=market.accepting_orders,
    )


async def poll_once() -> int:
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            select(MarketActive.condition_id).order_by(MarketActive.rank)
        )
        condition_ids = [row[0] for row in result.all()]

    if not condition_ids:
        log.warning("poller.empty_universe")
        return 0

    written = 0
    fetched: list[tuple[str, GammaMarket]] = []

    # 1) Fetch en batch (1 request por chunk en vez de 1 por mercado),
    #    con fallback individual para los IDs que no vuelvan en el batch.
    async with GammaClient(base_url=settings.polymarket_gamma_url) as client:
        try:
            markets = await client.get_markets_by_condition_ids(condition_ids)
        except Exception as exc:
            log.warning(
                "poller.gamma_unavailable",
                error=repr(exc),
                universe_size=len(condition_ids),
            )
            return 0
        for cid in (c for c in condition_ids if c not in markets):
            try:
                m = await client.get_market_by_condition_id(cid)
            except Exception as exc:
                log.warning("poller.fetch_failed", condition_id=cid, error=repr(exc))
                continue
            if m is not None:
                markets[cid] = m

    # 2) Leer los books que exec haya publicado. Si Redis falla, seguimos con
    #    Gamma a secas: es exactamente el comportamiento pre-Fase 1.
    ws_books: dict[str, CachedBook] = {}
    for cid in condition_ids:
        if cid not in markets:
            continue
        try:
            book = await get_book(cid)
        except Exception as exc:
            log.warning("poller.book_read_failed", condition_id=cid, error=repr(exc))
            continue
        if is_usable_ws_book(book):
            ws_books[cid] = book  # type: ignore[assignment]

    # 3) Persistir cada snapshot en su propio SAVEPOINT: un fallo de DB en un
    #    mercado no envenena la transacción ni tira el tick completo.
    async with sm() as session:
        for cid in condition_ids:
            m = markets.get(cid)
            if m is None:
                continue
            try:
                async with session.begin_nested():
                    session.add(build_snapshot(cid, m, ws_books.get(cid)))
                fetched.append((cid, m))
                written += 1
            except Exception as exc:
                log.warning("poller.persist_failed", condition_id=cid, error=repr(exc))
        await session.commit()

    # 4) Refrescar el cache SOLO para los mercados sin book de WS. Escribir aquí
    #    encima de un book del WebSocket sería cambiar profundidad real por top of
    #    book de hace 30s, y además borraría `bids`/`asks`.
    cached = 0
    for cid, m in fetched:
        if cid in ws_books:
            continue
        try:
            await set_book(
                CachedBook(
                    condition_id=cid,
                    ts=now_iso(),
                    best_bid=m.best_bid,
                    best_ask=m.best_ask,
                    last_trade_price=m.last_trade_price,
                    spread=m.spread,
                    liquidity_num=m.liquidity_num,
                    volume_24hr=m.volume_24hr,
                )
            )
            cached += 1
        except Exception as exc:
            log.warning("poller.cache_failed", condition_id=cid, error=repr(exc))

    signals_emitted = 0
    async with sm() as session:
        for cid, _ in fetched:
            try:
                async with session.begin_nested():
                    sig = await evaluate_market(session, cid)
                    accepted = sig is not None and sig.accepted
                if accepted:
                    signals_emitted += 1
            except Exception as exc:
                log.warning(
                    "poller.evaluate_failed", condition_id=cid, error=repr(exc)
                )
        await session.commit()

    log.info(
        "poller.tick",
        written=written,
        cached=cached,
        from_ws=len(ws_books),
        signals=signals_emitted,
        universe_size=len(condition_ids),
    )
    return written


async def poller_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await poll_once()
        except Exception as exc:
            log.error("poller.tick_failed", error=repr(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.poll_interval_sec)
        except TimeoutError:
            continue
