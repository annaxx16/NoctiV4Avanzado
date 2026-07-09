"""Orquestador de señales: edge → probability → risk + sizer → persist + stream.

Llamado por el poller después de cada snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from umbra.cache.redis_client import get_redis
from umbra.config import settings
from umbra.db.models import Signal
from umbra.edges.momentum import detect as detect_momentum
from umbra.edges.overreaction import detect as detect_overreaction
from umbra.engine.probability import compute_p_fair
from umbra.execution.paper import execute_signal as paper_execute
from umbra.features.loader import load_snapshots
from umbra.logging import get_logger
from umbra.risk.engine import check as risk_check
from umbra.risk.sizer import size_position
from umbra.ta.signal import evaluate_entry as ta_evaluate_entry

log = get_logger("umbra.orchestrator")
SIGNAL_STREAM_KEY = "umbra:signals"


def _dec(x: float) -> Decimal:
    return Decimal(str(x))


async def _emit_to_stream(signal_dict: dict) -> None:
    try:
        redis = get_redis()
        payload = {k: ("" if v is None else str(v)) for k, v in signal_dict.items()}
        await redis.xadd(SIGNAL_STREAM_KEY, payload, maxlen=10_000, approximate=True)
    except Exception as exc:
        log.warning("orchestrator.stream_failed", error=repr(exc))


async def evaluate_market(
    session: AsyncSession, condition_id: str
) -> Signal | None:
    as_of = datetime.now(UTC)
    snapshots = await load_snapshots(session, condition_id, as_of)

    edge = detect_overreaction(snapshots, as_of)
    if edge is None:
        edge = detect_momentum(snapshots, as_of)
    if edge is None:
        return None

    # Gate TA: si la tendencia técnica contradice fuerte, rechazamos sin pasar
    # por el risk engine (queda persistido en Signal como rechazo).
    ta_verdict = await ta_evaluate_entry(session, condition_id, edge.side)

    p_fair_yes = compute_p_fair(edge)
    sizing = size_position(
        side=edge.side,
        p_fair_yes=p_fair_yes,
        market_price_yes=edge.market_price,
    )

    if ta_verdict.reject:
        signal = Signal(
            ts=as_of,
            market_id=condition_id,
            edge_name=edge.edge_name,
            side=edge.side,
            market_price=_dec(edge.market_price),
            fair_price=_dec(edge.fair_price),
            edge_value=_dec(edge.edge_value),
            strength=_dec(edge.strength),
            size_shares=None,
            notional_usd=None,
            accepted=False,
            reason=ta_verdict.reason,
            mode=settings.mode,
        )
        session.add(signal)
        await session.flush()
        log.info(
            "signal.rejected_by_ta",
            condition_id=condition_id,
            side=edge.side,
            reason=ta_verdict.reason,
        )
        return signal

    decision = await risk_check(
        session,
        condition_id,
        edge.edge_value,
        sizing,
        side=edge.side,
        confidence=ta_verdict.confidence,
    )

    signal = Signal(
        ts=as_of,
        market_id=condition_id,
        edge_name=edge.edge_name,
        side=edge.side,
        market_price=_dec(edge.market_price),
        fair_price=_dec(edge.fair_price),
        edge_value=_dec(edge.edge_value),
        strength=_dec(edge.strength),
        size_shares=_dec(decision.adjusted_shares) if decision.accepted else None,
        notional_usd=_dec(decision.adjusted_notional_usd) if decision.accepted else None,
        accepted=decision.accepted,
        reason=decision.reason,
        mode=settings.mode,
    )
    session.add(signal)
    await session.flush()

    if decision.accepted:
        await _emit_to_stream(
            {
                "id": signal.id,
                "ts": as_of.isoformat(),
                "market_id": condition_id,
                "side": edge.side,
                "market_price": edge.market_price,
                "fair_price": edge.fair_price,
                "edge_value": edge.edge_value,
                "strength": edge.strength,
                "size_shares": decision.adjusted_shares,
                "notional_usd": decision.adjusted_notional_usd,
                "mode": settings.mode,
            }
        )
        log.info(
            "signal.accepted",
            condition_id=condition_id,
            side=edge.side,
            edge=edge.edge_value,
            sigma=edge.strength,
            notional=decision.adjusted_notional_usd,
        )

        # En modos sim/paper, simulamos el fill contra el book real
        if settings.mode in {"sim", "paper"}:
            try:
                liquidity = _liquidity_from_snapshots(snapshots)
                await paper_execute(session, signal, liquidity_usd=liquidity)
            except Exception as exc:
                log.warning(
                    "paper.execute_failed",
                    condition_id=condition_id,
                    signal_id=signal.id,
                    error=repr(exc),
                )
    else:
        log.info(
            "signal.rejected",
            condition_id=condition_id,
            reason=decision.reason,
        )

    return signal


def _liquidity_from_snapshots(snapshots) -> float | None:
    """Obtiene la liquidez más reciente del histórico (último snapshot)."""
    if not snapshots:
        return None
    for s in reversed(snapshots):
        if hasattr(s, "volume_24hr") and s.volume_24hr is not None:
            return float(s.volume_24hr)
    return None
