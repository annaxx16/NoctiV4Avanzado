"""OHLC aggregator loop — agrega snapshots a velas y persiste por intervalo."""

from __future__ import annotations

import asyncio

from umbra.config import settings
from umbra.db.session import get_sessionmaker
from umbra.logging import get_logger
from umbra.ta.ohlc import aggregate_and_persist_universe

log = get_logger("umbra.ohlc_loop")


async def ohlc_tick() -> int:
    sm = get_sessionmaker()
    async with sm() as session:
        n = await aggregate_and_persist_universe(session)
        await session.commit()
    log.info("ohlc_loop.tick", n_bars=n)
    return n


async def ohlc_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await ohlc_tick()
        except Exception as exc:
            log.error("ohlc_loop.tick_failed", error=repr(exc))
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.ohlc_aggregator_interval_sec
            )
        except TimeoutError:
            continue
