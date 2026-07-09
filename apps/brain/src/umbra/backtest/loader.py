"""Carga de datos históricos desde Postgres para el backtester.

Convierte filas de `book_snapshots` en `SnapshotInput` y lee `outcomes`. Es la
única pieza del paquete `backtest` con I/O; el resto es puro.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.db.models import BookSnapshot, Outcome
from umbra.features.calculator import SnapshotInput


def _f(v) -> float | None:
    return float(v) if v is not None else None


async def load_backtest_data(
    session: AsyncSession,
    *,
    condition_ids: list[str] | None = None,
    since: datetime | None = None,
) -> tuple[dict[str, list[SnapshotInput]], dict[str, bool]]:
    """Devuelve (markets, outcomes) listos para `run_backtest`/`walk_forward`."""
    stmt = select(BookSnapshot)
    if condition_ids:
        stmt = stmt.where(BookSnapshot.market_id.in_(condition_ids))
    if since is not None:
        stmt = stmt.where(BookSnapshot.ts >= since)
    stmt = stmt.order_by(BookSnapshot.market_id, BookSnapshot.ts)

    markets: dict[str, list[SnapshotInput]] = {}
    for row in (await session.execute(stmt)).scalars():
        markets.setdefault(row.market_id, []).append(
            SnapshotInput(
                ts=row.ts,
                best_bid=_f(row.best_bid),
                best_ask=_f(row.best_ask),
                last_trade_price=_f(row.last_trade_price),
                spread=_f(row.spread),
                volume_24hr=_f(row.volume_24hr),
            )
        )

    out_stmt = select(Outcome.market_id, Outcome.yes_outcome)
    if condition_ids:
        out_stmt = out_stmt.where(Outcome.market_id.in_(condition_ids))
    outcomes = dict((await session.execute(out_stmt)).all())

    return markets, outcomes
