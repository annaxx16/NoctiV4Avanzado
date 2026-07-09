"""Daily statistical learning loop."""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.analytics.edge_performance import refresh_edge_performance
from umbra.analytics.edge_weights import refresh_edge_weights
from umbra.db.models import EdgePerformance, EdgeWeight, LearningSnapshot


def _perf_payload(row: EdgePerformance) -> dict:
    return {
        "edge_name": row.edge_name,
        "signals_generated": row.signals_generated,
        "signals_accepted": row.signals_accepted,
        "trades_executed": row.trades_executed,
        "wins": row.wins,
        "losses": row.losses,
        "expectancy": float(row.expectancy) if row.expectancy is not None else None,
        "profit_factor": float(row.profit_factor) if row.profit_factor is not None else None,
        "sharpe": float(row.sharpe) if row.sharpe is not None else None,
        "max_drawdown": float(row.max_drawdown) if row.max_drawdown is not None else None,
    }


def _weight_payload(row: EdgeWeight) -> dict:
    return {
        "edge_name": row.edge_name,
        "weight": float(row.weight),
        "raw_score": float(row.raw_score),
        "applied_to_execution": False,
    }


async def run_learning_once(session: AsyncSession) -> LearningSnapshot:
    try:
        performance = await refresh_edge_performance(session)
        weights = await refresh_edge_weights(session)
        snapshot = LearningSnapshot(
            status="ok",
            edges_evaluated=len(performance),
            weights_updated=len(weights),
            report_json={
                "performance": [_perf_payload(row) for row in performance],
                "weights": [_weight_payload(row) for row in weights],
            },
        )
        session.add(snapshot)
        await session.flush()
        return snapshot
    except Exception as exc:
        snapshot = LearningSnapshot(
            status="error",
            edges_evaluated=0,
            weights_updated=0,
            report_json=None,
            error=repr(exc),
        )
        session.add(snapshot)
        await session.flush()
        return snapshot


async def latest_learning_snapshot(session: AsyncSession) -> LearningSnapshot | None:
    return (
        await session.execute(
            select(LearningSnapshot).order_by(desc(LearningSnapshot.ts)).limit(1)
        )
    ).scalar_one_or_none()
