"""Signal audit helpers.

This module mirrors persisted Signal rows into a richer audit table without
changing trading decisions. It keeps the rejection taxonomy centralized so API
and dashboard code can report the same categories the engine records.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.analytics.edge_performance import refresh_edge_performance
from umbra.db.models import Market, Signal, SignalAudit


@dataclass(frozen=True)
class RejectionFlags:
    risk_blocked: bool = False
    liquidity_blocked: bool = False
    exposure_blocked: bool = False
    composite_blocked: bool = False
    execution_blocked: bool = False


def classify_rejection(reason: str | None) -> RejectionFlags:
    if not reason:
        return RejectionFlags()

    r = reason.lower()
    liquidity_terms = (
        "liquidity",
        "spread",
        "stale_book",
        "no_book_snapshot",
        "too_close_to_resolution",
    )
    exposure_terms = (
        "exposure",
        "position_already_open",
        "cash_reserve",
        "max_risk",
    )
    risk_terms = (
        "kill_switch",
        "auto_halt",
        "dd ",
        "drawdown",
        "cooldown",
        "edge ",
        "kelly",
        "confidence",
        "ta_",
    )
    execution_terms = ("execute", "fill", "execution")
    composite_terms = ("composite",)

    return RejectionFlags(
        risk_blocked=any(term in r for term in risk_terms),
        liquidity_blocked=any(term in r for term in liquidity_terms),
        exposure_blocked=any(term in r for term in exposure_terms),
        composite_blocked=any(term in r for term in composite_terms),
        execution_blocked=any(term in r for term in execution_terms),
    )


async def audit_signal(
    session: AsyncSession,
    signal: Signal,
    *,
    metadata: dict | None = None,
) -> SignalAudit:
    market_name = (
        await session.execute(
            select(Market.question).where(Market.condition_id == signal.market_id)
        )
    ).scalar_one_or_none()

    rejected = not signal.accepted
    flags = classify_rejection(signal.reason if rejected else None)
    audit = SignalAudit(
        signal_id=signal.id,
        timestamp=signal.ts,
        market_id=signal.market_id,
        market_name=market_name,
        edge_name=signal.edge_name,
        score=signal.edge_value,
        direction=signal.side,
        accepted=signal.accepted,
        rejected=rejected,
        rejected_reason=signal.reason if rejected else None,
        risk_blocked=flags.risk_blocked,
        liquidity_blocked=flags.liquidity_blocked,
        exposure_blocked=flags.exposure_blocked,
        composite_blocked=flags.composite_blocked,
        execution_blocked=flags.execution_blocked,
        metadata_json=metadata,
    )
    session.add(audit)
    await session.flush()
    await refresh_edge_performance(session, signal.edge_name)
    return audit
