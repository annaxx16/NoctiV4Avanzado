"""Exit loop — evalúa periódicamente todas las posiciones abiertas y ejecuta cierres."""

from __future__ import annotations

import asyncio

from umbra.config import settings
from umbra.db.session import get_sessionmaker
from umbra.engine.exit_engine import evaluate_and_execute_exits
from umbra.logging import get_logger
from umbra.portfolio.manager import portfolio_snapshot

log = get_logger("umbra.exit_loop")


async def exit_tick() -> int:
    sm = get_sessionmaker()
    async with sm() as session:
        snap = await portfolio_snapshot(session)
        decisions = await evaluate_and_execute_exits(session, snap.drawdown_pct)
        await session.commit()
    if decisions:
        log.info(
            "exit_loop.tick",
            n_exits=len(decisions),
            reasons=[d.reason for d in decisions],
            dd_pct=snap.drawdown_pct,
        )
    return len(decisions)


async def exit_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await exit_tick()
        except Exception as exc:
            log.error("exit_loop.tick_failed", error=repr(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.exit_loop_interval_sec)
        except TimeoutError:
            continue
