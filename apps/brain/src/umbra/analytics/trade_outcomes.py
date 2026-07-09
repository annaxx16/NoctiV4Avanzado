"""Trade outcome recording.

Each CLOSE fill gets a normalized outcome row. This closes the loop from:
Signal -> OPEN fill -> CLOSE fill -> realized outcome.
"""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.analytics.edge_performance import refresh_edge_performance
from umbra.db.models import PaperFill, PaperPosition, Signal, TradeOutcome


async def _entry_context(
    session: AsyncSession, market_id: str, side: str
) -> tuple[PaperFill | None, Signal | None]:
    row = (
        await session.execute(
            select(PaperFill, Signal)
            .outerjoin(Signal, Signal.id == PaperFill.signal_id)
            .where(
                PaperFill.market_id == market_id,
                PaperFill.side == side,
                PaperFill.action == "OPEN",
            )
            .order_by(desc(PaperFill.ts))
            .limit(1)
        )
    ).first()
    if row is None:
        return None, None
    return row[0], row[1]


async def record_trade_outcome(
    session: AsyncSession,
    *,
    close_fill: PaperFill,
    position: PaperPosition,
    cost_basis_released: Decimal,
    realized_pnl: Decimal,
    exit_reason: str,
    market_conditions: dict | None = None,
) -> TradeOutcome:
    entry_fill, entry_signal = await _entry_context(
        session, close_fill.market_id, close_fill.side
    )

    opened_at = position.opened_at
    closed_at = close_fill.ts
    if opened_at is not None and opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=UTC)
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=UTC)

    holding_hours = None
    if opened_at is not None:
        holding_hours = Decimal(str((closed_at - opened_at).total_seconds() / 3600.0))

    return_pct = None
    if cost_basis_released > 0:
        return_pct = realized_pnl / cost_basis_released

    profit = realized_pnl if realized_pnl > 0 else Decimal("0")
    loss = -realized_pnl if realized_pnl < 0 else Decimal("0")

    outcome = TradeOutcome(
        close_fill_id=close_fill.id,
        entry_signal_id=entry_signal.id if entry_signal is not None else None,
        market_id=close_fill.market_id,
        side=close_fill.side,
        opened_at=opened_at,
        closed_at=closed_at,
        entry_price=entry_fill.fill_price if entry_fill is not None else position.avg_entry_price,
        exit_price=close_fill.fill_price,
        holding_time_hours=holding_hours,
        return_pct=return_pct,
        profit_usd=profit,
        loss_usd=loss,
        realized_pnl_usd=realized_pnl,
        winning_trade=realized_pnl > 0,
        losing_trade=realized_pnl < 0,
        edge_source=entry_signal.edge_name if entry_signal is not None else None,
        exit_reason=exit_reason,
        market_conditions=market_conditions,
        mode=close_fill.mode,
    )
    session.add(outcome)
    await session.flush()
    if outcome.edge_source is not None:
        await refresh_edge_performance(session, outcome.edge_source)
    return outcome
