"""Dynamic edge weighting.

Weights are derived from performance metrics but are not applied to live
execution yet. They are capped to reduce overfitting pressure.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.db.models import EdgePerformance, EdgeWeight

MIN_WEIGHT = 0.05
MAX_WEIGHT = 0.35


def _float(value) -> float | None:
    return None if value is None else float(value)


def _positive(value: float | None, default: float = 0.0) -> float:
    if value is None or not math.isfinite(value):
        return default
    return max(0.0, value)


def _rolling_score(payload: dict | None) -> float:
    if not payload or not payload.get("trades"):
        return 0.0
    expectancy = _positive(payload.get("expectancy"))
    pf = _positive(payload.get("profit_factor"), default=1.0)
    sharpe = _positive(payload.get("sharpe"), default=1.0)
    return max(0.0, expectancy) * max(0.25, pf) * max(0.25, sharpe)


def score_edge(perf: EdgePerformance) -> dict:
    pf = _positive(_float(perf.profit_factor), default=1.0)
    expectancy = _positive(_float(perf.expectancy))
    sharpe = _positive(_float(perf.sharpe), default=1.0)
    max_dd = _positive(_float(perf.max_drawdown))
    stability = 1.0 / (1.0 + max_dd)
    rolling_30 = _rolling_score(perf.rolling_30d)
    rolling_100 = _rolling_score(perf.rolling_100_trades)

    if perf.trades_executed <= 0:
        raw = 0.0
    else:
        recency = max(rolling_30, rolling_100, 0.25)
        raw = pf * expectancy * sharpe * stability * recency

    return {
        "raw_score": raw,
        "profit_factor": pf,
        "expectancy": expectancy,
        "sharpe": sharpe,
        "stability_score": stability,
        "rolling_30d_score": rolling_30,
        "rolling_100_trades_score": rolling_100,
    }


def _bounded_normalized(scores: list[float]) -> list[float]:
    if not scores:
        return []
    total = sum(scores)
    if total <= 0:
        return [MIN_WEIGHT for _ in scores]
    return [min(MAX_WEIGHT, max(MIN_WEIGHT, s / total)) for s in scores]


async def refresh_edge_weights(session: AsyncSession) -> list[EdgeWeight]:
    perfs = (
        await session.execute(select(EdgePerformance).order_by(EdgePerformance.edge_name))
    ).scalars().all()
    scored = [(perf, score_edge(perf)) for perf in perfs]
    weights = _bounded_normalized([s["raw_score"] for _, s in scored])
    now = datetime.now(UTC)

    rows: list[EdgeWeight] = []
    for (perf, score), weight in zip(scored, weights, strict=False):
        row = await session.get(EdgeWeight, perf.edge_name)
        if row is None:
            row = EdgeWeight(edge_name=perf.edge_name)
            session.add(row)
        row.raw_score = Decimal(str(score["raw_score"]))
        row.weight = Decimal(str(weight))
        row.profit_factor = Decimal(str(score["profit_factor"]))
        row.expectancy = Decimal(str(score["expectancy"]))
        row.sharpe = Decimal(str(score["sharpe"]))
        row.stability_score = Decimal(str(score["stability_score"]))
        row.rolling_30d_score = Decimal(str(score["rolling_30d_score"]))
        row.rolling_100_trades_score = Decimal(str(score["rolling_100_trades_score"]))
        row.metadata_json = {
            "min_weight": MIN_WEIGHT,
            "max_weight": MAX_WEIGHT,
            "applied_to_execution": False,
            "reason": "informational until Composite Engine is active",
        }
        row.updated_at = now
        rows.append(row)

    await session.flush()
    return rows


async def latest_edge_weights(session: AsyncSession) -> list[EdgeWeight]:
    return (
        await session.execute(select(EdgeWeight).order_by(EdgeWeight.weight.desc()))
    ).scalars().all()
