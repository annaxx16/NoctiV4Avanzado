"""Outcomes loop — resuelve mercados vencidos cada N segundos (default 1h)."""

from __future__ import annotations

import asyncio

from umbra.config import settings
from umbra.db.session import get_sessionmaker
from umbra.logging import get_logger
from umbra.validation.outcome_resolver import resolve_pending_outcomes

log = get_logger("umbra.outcomes_loop")


async def outcomes_tick() -> int:
    sm = get_sessionmaker()
    async with sm() as session:
        n = await resolve_pending_outcomes(session)
        await session.commit()
    if n:
        log.info("outcomes_loop.tick", resolved=n)
    return n


async def outcomes_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await outcomes_tick()
        except Exception as exc:
            log.warning("outcomes_loop.tick_failed", error=repr(exc))
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.outcomes_resolver_interval_sec
            )
        except TimeoutError:
            continue
