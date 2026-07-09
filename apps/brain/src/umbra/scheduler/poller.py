"""Poller: cada N segundos, para cada mercado del universo descarga snapshot
desde Gamma y lo persiste en book_snapshots.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from umbra.cache.book_cache import CachedBook, now_iso, set_book
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


def _to_snapshot(condition_id: str, m: GammaMarket) -> BookSnapshot:
    return BookSnapshot(
        market_id=condition_id,
        ts=datetime.now(UTC),
        best_bid=_dec(m.best_bid),
        best_ask=_dec(m.best_ask),
        last_trade_price=_dec(m.last_trade_price),
        spread=_dec(m.spread),
        liquidity_num=_dec(m.liquidity_num),
        volume_24hr=_dec(m.volume_24hr),
        active=m.active,
        accepting_orders=m.accepting_orders,
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

    # 2) Persistir cada snapshot en su propio SAVEPOINT: un fallo de DB en un
    #    mercado no envenena la transacción ni tira el tick completo.
    async with sm() as session:
        for cid in condition_ids:
            m = markets.get(cid)
            if m is None:
                continue
            try:
                async with session.begin_nested():
                    session.add(_to_snapshot(cid, m))
                fetched.append((cid, m))
                written += 1
            except Exception as exc:
                log.warning("poller.persist_failed", condition_id=cid, error=repr(exc))
        await session.commit()

    cached = 0
    for cid, m in fetched:
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
