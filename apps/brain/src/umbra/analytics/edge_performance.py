"""Edge performance aggregation."""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.backtest import metrics
from umbra.config import settings
from umbra.db.models import EdgePerformance, SignalAudit, TradeOutcome


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _avg(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _profit_factor(pnls: list[float]) -> float | None:
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses == 0:
        return None if gains > 0 else 0.0
    return gains / losses


def _sharpe(returns: list[float]) -> float | None:
    """Wrapper sobre `metrics.sharpe` que conserva el None de este módulo.

    `metrics.sharpe` devuelve 0.0 en los casos degenerados (<2 trades, std=0);
    aquí se persiste en una columna nullable y `edge_weights` distingue "sin
    dato" (usa default=1.0) de "cero". Mantener esa distinción.
    """
    if len(returns) < 2 or statistics.stdev(returns) == 0:
        return None
    return metrics.sharpe(returns)


def _max_drawdown(pnls: list[float]) -> float:
    """Drawdown fraccional sobre el bankroll configurado.

    Delega en `metrics.max_drawdown` en vez de reimplementarlo: esta copia
    arrastraba el mismo bug de equity partiendo de 0, y aquí dolía más que en
    el backtest — alimenta `edge_weights.score_edge`, donde un max_dd falso de
    0.0 se traduce en `stability = 1/(1+0) = 1.0`, la puntuación máxima. Un
    edge que casi revienta y remonta puntuaba como uno estable.
    """
    return metrics.max_drawdown(pnls, settings.bankroll_usd)


def _summary(trades: list[TradeOutcome]) -> dict:
    pnls = [float(t.realized_pnl_usd) for t in trades]
    returns = [float(t.return_pct) for t in trades if t.return_pct is not None]
    pf = _profit_factor(pnls)
    return {
        "trades": len(trades),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p < 0),
        "avg_return": _avg(returns),
        "profit_factor": pf if pf is None or math.isfinite(pf) else None,
        "sharpe": _sharpe(returns),
        "expectancy": _avg(pnls),
        "max_drawdown": _max_drawdown(pnls),
        "total_pnl_usd": sum(pnls),
    }


async def _edge_names(session: AsyncSession, edge_name: str | None) -> list[str]:
    if edge_name:
        return [edge_name]
    audit_edges = (
        await session.execute(select(SignalAudit.edge_name).distinct())
    ).scalars().all()
    outcome_edges = (
        await session.execute(
            select(TradeOutcome.edge_source).where(TradeOutcome.edge_source.is_not(None)).distinct()
        )
    ).scalars().all()
    return sorted({*audit_edges, *outcome_edges})


async def refresh_edge_performance(
    session: AsyncSession, edge_name: str | None = None
) -> list[EdgePerformance]:
    refreshed: list[EdgePerformance] = []
    now = datetime.now(UTC)

    for edge in await _edge_names(session, edge_name):
        signals_generated = (
            await session.execute(
                select(func.count(SignalAudit.id)).where(SignalAudit.edge_name == edge)
            )
        ).scalar() or 0
        signals_accepted = (
            await session.execute(
                select(func.count(SignalAudit.id)).where(
                    SignalAudit.edge_name == edge,
                    SignalAudit.accepted.is_(True),
                )
            )
        ).scalar() or 0

        trades = (
            await session.execute(
                select(TradeOutcome)
                .where(TradeOutcome.edge_source == edge)
                .order_by(TradeOutcome.closed_at)
            )
        ).scalars().all()
        summary = _summary(trades)

        rolling_7d = _summary(
            [t for t in trades if t.closed_at >= now - timedelta(days=7)]
        )
        rolling_30d = _summary(
            [t for t in trades if t.closed_at >= now - timedelta(days=30)]
        )
        rolling_100 = _summary(trades[-100:])

        row = await session.get(EdgePerformance, edge)
        if row is None:
            row = EdgePerformance(edge_name=edge)
            session.add(row)

        row.signals_generated = signals_generated
        row.signals_accepted = signals_accepted
        row.trades_executed = summary["trades"]
        row.wins = summary["wins"]
        row.losses = summary["losses"]
        row.avg_return = _dec(summary["avg_return"])
        row.profit_factor = _dec(summary["profit_factor"])
        row.sharpe = _dec(summary["sharpe"])
        row.expectancy = _dec(summary["expectancy"])
        row.max_drawdown = _dec(summary["max_drawdown"])
        row.rolling_7d = rolling_7d
        row.rolling_30d = rolling_30d
        row.rolling_100_trades = rolling_100
        row.updated_at = now
        refreshed.append(row)

    await session.flush()
    return refreshed


async def latest_edge_performance(session: AsyncSession) -> list[EdgePerformance]:
    return (
        await session.execute(
            select(EdgePerformance).order_by(desc(EdgePerformance.expectancy))
        )
    ).scalars().all()
