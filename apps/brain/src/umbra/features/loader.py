"""Carga snapshots desde Postgres y los convierte a SnapshotInput."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.db.models import BookSnapshot
from umbra.features.calculator import SnapshotInput

DEFAULT_LOOKBACK = timedelta(minutes=30)


async def load_snapshots(
    session: AsyncSession,
    condition_id: str,
    as_of: datetime,
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> list[SnapshotInput]:
    stmt = (
        select(BookSnapshot)
        .where(BookSnapshot.market_id == condition_id)
        .where(BookSnapshot.ts >= as_of - lookback)
        .where(BookSnapshot.ts <= as_of)
        .order_by(BookSnapshot.ts.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        SnapshotInput(
            ts=row.ts,
            best_bid=float(row.best_bid) if row.best_bid is not None else None,
            best_ask=float(row.best_ask) if row.best_ask is not None else None,
            last_trade_price=float(row.last_trade_price)
            if row.last_trade_price is not None
            else None,
            spread=float(row.spread) if row.spread is not None else None,
            volume_24hr=float(row.volume_24hr) if row.volume_24hr is not None else None,
        )
        for row in rows
    ]
