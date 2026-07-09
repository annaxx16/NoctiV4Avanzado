"""Daily learning loop: refresh edge performance and weights."""

from __future__ import annotations

import asyncio

from umbra.analytics.learning import run_learning_once
from umbra.db.session import get_sessionmaker
from umbra.logging import get_logger

log = get_logger("umbra.learning")

LEARNING_INTERVAL_SEC = 24 * 60 * 60


async def learning_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            sm = get_sessionmaker()
            async with sm() as session:
                snap = await run_learning_once(session)
                await session.commit()
                log.info(
                    "learning.snapshot",
                    status=snap.status,
                    edges=snap.edges_evaluated,
                    weights=snap.weights_updated,
                )
        except Exception as exc:
            log.error("learning.loop_failed", error=repr(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=LEARNING_INTERVAL_SEC)
        except TimeoutError:
            continue
