"""Equity snapshot loop — persiste un punto de la curva real cada N segundos."""

from __future__ import annotations

import asyncio

from umbra.config import settings
from umbra.db.session import get_sessionmaker
from umbra.logging import get_logger
from umbra.portfolio.manager import persist_equity_snapshot

log = get_logger("umbra.equity_loop")


async def equity_tick() -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        row = await persist_equity_snapshot(session)
        await session.commit()
        log.info(
            "equity_loop.snapshot",
            equity=float(row.equity_usd),
            unrealized=float(row.unrealized_pnl_usd),
            realized_total=float(row.realized_pnl_usd_total),
            dd_pct=float(row.drawdown_pct),
            gross=float(row.gross_exposure_usd),
            n_open=row.n_open_positions,
        )


async def equity_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await equity_tick()
        except Exception as exc:
            log.error("equity_loop.tick_failed", error=repr(exc))
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.equity_snapshot_interval_sec
            )
        except TimeoutError:
            continue
