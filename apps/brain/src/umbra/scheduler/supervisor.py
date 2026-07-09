"""Supervisor — chequea drawdown y activa halt+flatten automático.

Si DD <= -dd_halt_pct y el kill switch aún no está activo:
  1. Activa kill switch
  2. Flatten total

Idempotente: si el halt ya está activo o no hay posiciones, no hace nada.
"""

from __future__ import annotations

import asyncio

from umbra.config import settings
from umbra.db.session import get_sessionmaker
from umbra.engine.exit_engine import flatten_all
from umbra.logging import get_logger
from umbra.portfolio.manager import portfolio_snapshot
from umbra.risk.engine import is_halted, set_halt

log = get_logger("umbra.supervisor")


async def supervisor_tick() -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        snap = await portfolio_snapshot(session)
    if snap.drawdown_pct <= -settings.dd_halt_pct:
        already = await is_halted()
        if not already:
            log.error(
                "supervisor.auto_halt",
                dd_pct=snap.drawdown_pct,
                equity=snap.equity_usd,
                peak=snap.peak_equity_usd,
            )
            await set_halt(True)
        # Flatten siempre que estemos en zona de halt — idempotente si ya 0 posiciones
        async with sm() as session:
            n = await flatten_all(session, reason="supervisor_auto_halt")
            await session.commit()
        if n > 0:
            log.error("supervisor.flatten_all", n=n)


async def supervisor_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await supervisor_tick()
        except Exception as exc:
            log.error("supervisor.tick_failed", error=repr(exc))
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.exit_loop_interval_sec
            )
        except TimeoutError:
            continue
