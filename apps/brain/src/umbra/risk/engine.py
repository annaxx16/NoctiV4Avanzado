"""Risk Engine v2 — gates institucionales antes de aceptar una señal.

Orden de evaluación (corta circuita en el primer fallo):
 1. kill switch (fail-CLOSED si Redis no responde)
 2. portfolio drawdown halt (DD <= -dd_halt_pct → bloqueo total)
 3. portfolio drawdown throttle (DD <= -dd_throttle_pct → kappa × 0.5)
 4. no-averaging-down (ya hay posición abierta en mismo market+side)
 5. cooldown post-exit (mismo mercado dentro de cooldown_minutes)
 6. liquidity / spread del snapshot actual
 6.5 time-to-resolution floor (rechaza mercados que resuelven en < N horas)
 7. min_edge / kelly > 0
 8. max_risk_per_trade_usd
 9. max_exposure_per_market_usd (incluye posición abierta)
10. portfolio gross exposure cap (max_gross_exposure_pct × bankroll)
11. cash reserve cap (cash post-trade >= min_cash_reserve_pct × bankroll)

Si pasa → devuelve RiskDecision(accepted=True, ...). Cualquier `reason` distinto
de "ok" se persiste en Signal.reason para auditoría.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.cache.redis_client import get_redis
from umbra.config import settings
from umbra.db.models import (
    BookSnapshot,
    Market,
    PaperFill,
    PaperPosition,
)
from umbra.logging import get_logger
from umbra.risk.sizer import SizingResult

log = get_logger("umbra.risk")

KILL_SWITCH_KEY = "umbra:halt"
HALT_REASON_KEY = "umbra:halt:reason"


@dataclass(frozen=True)
class RiskDecision:
    accepted: bool
    reason: str
    adjusted_notional_usd: float
    adjusted_shares: float
    kappa_factor: float = 1.0  # informativo; el sizer ya aplicó


# ---------------------------------------------------------------------------
# Kill switch — fail CLOSED: si Redis falla, asume halt activo.
# ---------------------------------------------------------------------------


async def is_halted() -> bool:
    redis = get_redis()
    try:
        val = await redis.get(KILL_SWITCH_KEY)
        return val == "1"
    except Exception as exc:
        fail_closed = settings.mode == "live" or settings.redis_fail_closed_in_sim
        log.warning(
            "risk.halt_check_failed",
            error=repr(exc),
            fail_closed=fail_closed,
            mode=settings.mode,
        )
        return fail_closed


async def halt_reason() -> str | None:
    redis = get_redis()
    try:
        return await redis.get(HALT_REASON_KEY)
    except Exception as exc:
        log.warning("risk.halt_reason_read_failed", error=repr(exc))
        return None


async def set_halt(active: bool, reason: str | None = None) -> None:
    redis = get_redis()
    try:
        if active:
            await redis.set(KILL_SWITCH_KEY, "1")
            if reason is not None:
                await redis.set(HALT_REASON_KEY, reason)
        else:
            await redis.delete(KILL_SWITCH_KEY)
            await redis.delete(HALT_REASON_KEY)
    except Exception as exc:
        log.error("risk.set_halt_failed", error=repr(exc), active=active)
        raise


# ---------------------------------------------------------------------------
# Portfolio queries (lightweight; no importan portfolio.manager para evitar ciclos)
# ---------------------------------------------------------------------------


async def open_position_for(
    session: AsyncSession, market_id: str, side: str
) -> PaperPosition | None:
    stmt = select(PaperPosition).where(
        PaperPosition.market_id == market_id,
        PaperPosition.side == side,
        PaperPosition.status == "open",
        PaperPosition.shares > 0,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def market_open_cost(session: AsyncSession, market_id: str) -> float:
    """Costo acumulado en posiciones abiertas del mercado (ambos lados)."""
    stmt = select(func.coalesce(func.sum(PaperPosition.total_cost_usd), 0)).where(
        PaperPosition.market_id == market_id,
        PaperPosition.status == "open",
    )
    return float((await session.execute(stmt)).scalar() or 0)


async def gross_exposure(session: AsyncSession) -> float:
    stmt = select(func.coalesce(func.sum(PaperPosition.total_cost_usd), 0)).where(
        PaperPosition.status == "open"
    )
    return float((await session.execute(stmt)).scalar() or 0)


async def realized_pnl_total(session: AsyncSession) -> float:
    stmt = select(func.coalesce(func.sum(PaperPosition.realized_pnl_usd), 0))
    return float((await session.execute(stmt)).scalar() or 0)


async def last_close_ts_for_market(
    session: AsyncSession, market_id: str
) -> datetime | None:
    stmt = (
        select(PaperFill.ts)
        .where(PaperFill.market_id == market_id, PaperFill.action == "CLOSE")
        .order_by(desc(PaperFill.ts))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def current_drawdown_pct(session: AsyncSession) -> float:
    """Calcula DD actual; evita bloquear por un EquitySnapshot viejo."""
    from umbra.portfolio.manager import portfolio_snapshot

    snap = await portfolio_snapshot(session)
    return snap.drawdown_pct


async def market_end_date(
    session: AsyncSession, market_id: str
) -> datetime | None:
    stmt = select(Market.end_date).where(Market.condition_id == market_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def fresh_book(
    session: AsyncSession, market_id: str
) -> BookSnapshot | None:
    stmt = (
        select(BookSnapshot)
        .where(BookSnapshot.market_id == market_id)
        .order_by(desc(BookSnapshot.ts))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------


async def check(
    session: AsyncSession,
    condition_id: str,
    edge_value: float,
    sizing: SizingResult,
    side: str | None = None,
    confidence: float | None = None,
) -> RiskDecision:
    # 1. Kill switch (fail-closed)
    if await is_halted():
        return RiskDecision(False, "kill_switch_active", 0.0, 0.0)

    # 2/3. Drawdown gates
    dd_pct = await current_drawdown_pct(session)  # típicamente negativo
    if dd_pct <= -settings.dd_halt_pct:
        # Auto-halt al detectar DD severo. Otra task del supervisor flatten-ea.
        await set_halt(True, reason="auto_dd")
        return RiskDecision(
            False,
            f"auto_halt_dd {dd_pct:.4f} <= -{settings.dd_halt_pct}",
            0.0,
            0.0,
        )
    kappa_factor = 1.0
    if dd_pct <= -settings.dd_throttle_pct:
        kappa_factor = 0.5

    # 4. No averaging down
    if side is not None:
        existing = await open_position_for(session, condition_id, side)
        if existing is not None:
            return RiskDecision(
                False,
                f"position_already_open shares={float(existing.shares):.4f}",
                0.0,
                0.0,
            )

    # 5. Cooldown post-exit
    last_close = await last_close_ts_for_market(session, condition_id)
    if last_close is not None:
        now = datetime.now(UTC)
        cooldown_until = last_close + timedelta(minutes=settings.cooldown_minutes)
        if now < cooldown_until:
            return RiskDecision(
                False, f"cooldown until {cooldown_until.isoformat()}", 0.0, 0.0
            )

    # 6. Liquidity + spread + book freshness
    book = await fresh_book(session, condition_id)
    if book is None:
        return RiskDecision(False, "no_book_snapshot", 0.0, 0.0)
    book_age = (datetime.now(UTC) - book.ts).total_seconds()
    if book_age > settings.stale_book_max_age_sec:
        return RiskDecision(False, f"stale_book age={book_age:.0f}s", 0.0, 0.0)
    if book.spread is not None and float(book.spread) > settings.max_spread_for_entry:
        return RiskDecision(
            False, f"spread_too_wide {float(book.spread):.4f}", 0.0, 0.0
        )
    # Fallback: si Gamma no devuelve liquidity_num, usar volume_24hr como proxy.
    liq_proxy = book.liquidity_num
    if liq_proxy is None:
        liq_proxy = book.volume_24hr
    if liq_proxy is None or float(liq_proxy) < settings.min_liquidity_for_entry_usd:
        return RiskDecision(
            False,
            f"liquidity_low {float(liq_proxy or 0):.0f}",
            0.0,
            0.0,
        )

    # 6.5 Time-to-resolution floor: no entrar en mercados a punto de resolver
    # (el salto a 0/1 nos arrolla y el edge de mean-reversion no tiene margen).
    end_date = await market_end_date(session, condition_id)
    if end_date is not None:
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=UTC)
        hours_to_res = (end_date - datetime.now(UTC)).total_seconds() / 3600.0
        if hours_to_res < settings.max_time_to_resolution_hours_floor:
            return RiskDecision(
                False,
                f"too_close_to_resolution {hours_to_res:.2f}h",
                0.0,
                0.0,
            )

    # 7. Min edge / Kelly válido
    if edge_value < settings.min_edge:
        return RiskDecision(
            False,
            f"edge {edge_value:.4f} < min_edge {settings.min_edge}",
            0.0,
            0.0,
        )
    if sizing.notional_usd <= 0 or sizing.shares <= 0:
        return RiskDecision(False, "kelly_zero_or_negative", 0.0, 0.0)

    # 7.5 Confidence (opcional)
    if confidence is not None and confidence < settings.min_signal_confidence:
        return RiskDecision(
            False, f"confidence {confidence:.2f} < min", 0.0, 0.0
        )

    notional = sizing.notional_usd * kappa_factor
    shares = sizing.shares * kappa_factor

    # 8. Max risk per trade
    if notional > settings.max_risk_per_trade_usd:
        ratio = settings.max_risk_per_trade_usd / notional
        notional = settings.max_risk_per_trade_usd
        shares = shares * ratio

    # 9. Per-market exposure (ya descontando posición abierta)
    market_cost = await market_open_cost(session, condition_id)
    market_room = settings.max_exposure_per_market_usd - market_cost
    if market_room <= 0:
        return RiskDecision(
            False,
            f"market_exposure_full {market_cost:.2f}",
            0.0,
            0.0,
        )
    if notional > market_room:
        ratio = market_room / notional
        notional = market_room
        shares = shares * ratio

    # 10. Gross exposure cap del portfolio
    gross = await gross_exposure(session)
    gross_cap = settings.bankroll_usd * settings.max_gross_exposure_pct
    gross_room = gross_cap - gross
    if gross_room <= 0:
        return RiskDecision(
            False, f"gross_exposure_full {gross:.2f}>={gross_cap:.2f}", 0.0, 0.0
        )
    if notional > gross_room:
        ratio = gross_room / notional
        notional = gross_room
        shares = shares * ratio

    # 11. Cash reserve (cash post-trade)
    realized = await realized_pnl_total(session)
    cash_now = settings.bankroll_usd + realized - gross
    cash_after = cash_now - notional
    min_reserve = settings.bankroll_usd * settings.min_cash_reserve_pct
    if cash_after < min_reserve:
        # ajustar para respetar reserva; si no queda nada → reject
        permitted = cash_now - min_reserve
        if permitted <= 0:
            return RiskDecision(
                False,
                f"cash_reserve_breach cash_now={cash_now:.2f}",
                0.0,
                0.0,
            )
        ratio = permitted / notional
        notional = permitted
        shares = shares * ratio

    return RiskDecision(True, "ok", notional, shares, kappa_factor=kappa_factor)


# ---------------------------------------------------------------------------
# Retro compat para llamadas existentes en orchestrator viejo
# (orchestrator.evaluate_market pasa 4 args). Vamos a hacer side opcional.
# ---------------------------------------------------------------------------

__all__ = [
    "KILL_SWITCH_KEY",
    "RiskDecision",
    "check",
    "is_halted",
    "set_halt",
    "open_position_for",
    "gross_exposure",
    "realized_pnl_total",
    "current_drawdown_pct",
]
