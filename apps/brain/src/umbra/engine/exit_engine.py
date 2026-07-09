"""Exit Engine — la pieza que faltaba.

Cada N segundos el background loop llama a `evaluate_and_execute_exits`. Para cada
PaperPosition abierta evaluamos triggers en orden de prioridad y si alguno dispara,
generamos un CLOSE vía `umbra.execution.paper.execute_close`.

Triggers (cortocircuito en el primer match):

  T0  stale_book          — sin precio fresco → cerrar al último conocido (riesgo: peor)
  T1  outcome_resolved    — el mercado resolvió: cerrar al outcome (1 o 0)
  T2  pre_resolution      — quedan < EXIT_BEFORE_RESOLUTION horas para cierre
  T3  portfolio_dd_halt   — drawdown global > umbral → flatten total
  T4  hard_stop_loss      — pnl_pct <= -SL_PCT
  T5  trailing_stop       — peak armed y giveback excedido
  T6  liquidity_degraded  — spread o liquidez actuales rotas vs entry
  T7  time_stop           — age >= TTL
  T8  take_profit         — pnl_pct >= TP_PCT
  T9  edge_invalidation   — el edge OverreactionV1 ahora apuntaría al lado opuesto
  T10 mean_revert_done    — precio cruzó la EMA al lado favorable → cerrar parcial 50%

Toda decisión se loggea con motivo. El "flatten" (kill-switch + DD halt) lo expone
`flatten_all` para que lo invoquen el supervisor o el admin endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.cache.book_cache import get_book as cache_get_book
from umbra.config import settings
from umbra.db.models import BookSnapshot, Market, Outcome, PaperPosition
from umbra.edges.common import ema as edge_ema
from umbra.edges.common import mid as edge_mid
from umbra.edges.overreaction import detect as detect_overreaction
from umbra.execution.paper import execute_close
from umbra.features.loader import load_snapshots
from umbra.logging import get_logger
from umbra.ta.signal import evaluate_exit_ta

log = get_logger("umbra.exit")


@dataclass(frozen=True)
class ExitDecision:
    market_id: str
    side: str
    reason: str  # 't1_outcome', 't4_stop_loss', etc.
    fraction: float  # 1.0 = total, 0.5 = mitad
    mark_price_yes: float  # mid_yes usado para el mark
    pnl_pct_at_decision: float
    age_hours: float


# ---------------------------------------------------------------------------
# Helpers de mark / pnl
# ---------------------------------------------------------------------------


def _side_price_from_mid(side: str, mid_yes: float) -> float:
    return mid_yes if side == "BUY_YES" else (1 - mid_yes)


def _unrealized_pnl(pos: PaperPosition, mid_yes: float) -> tuple[float, float]:
    """Devuelve (unrealized_pnl_usd, pnl_pct) sobre cost basis abierto."""
    if pos.shares <= 0 or pos.total_cost_usd <= 0:
        return 0.0, 0.0
    side_price = _side_price_from_mid(pos.side, mid_yes)
    cur_value = float(pos.shares) * side_price
    cost = float(pos.total_cost_usd)
    pnl = cur_value - cost
    pct = pnl / cost
    return pnl, pct


async def _resolve_mid_yes(
    session: AsyncSession, market_id: str
) -> tuple[float | None, datetime | None, BookSnapshot | None]:
    """Devuelve (mid_yes, observed_ts, latest_snapshot)."""
    cached = await cache_get_book(market_id)
    if (
        cached is not None
        and cached.best_bid is not None
        and cached.best_ask is not None
    ):
        return (cached.best_bid + cached.best_ask) / 2.0, datetime.fromisoformat(
            cached.ts
        ), None

    stmt = (
        select(BookSnapshot)
        .where(BookSnapshot.market_id == market_id)
        .order_by(desc(BookSnapshot.ts))
        .limit(1)
    )
    snap = (await session.execute(stmt)).scalar_one_or_none()
    if snap is None:
        return None, None, None
    if snap.best_bid is not None and snap.best_ask is not None:
        return float((snap.best_bid + snap.best_ask) / 2), snap.ts, snap
    if snap.last_trade_price is not None:
        return float(snap.last_trade_price), snap.ts, snap
    return None, snap.ts, snap


async def _market_end_date(session: AsyncSession, market_id: str) -> datetime | None:
    stmt = select(Market.end_date).where(Market.condition_id == market_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _outcome_for(session: AsyncSession, market_id: str) -> Outcome | None:
    return (
        await session.execute(select(Outcome).where(Outcome.market_id == market_id))
    ).scalar_one_or_none()


async def _spread_at_entry(
    session: AsyncSession, pos: PaperPosition
) -> float | None:
    """Spread del snapshot más cercano por debajo de opened_at."""
    stmt = (
        select(BookSnapshot.spread)
        .where(BookSnapshot.market_id == pos.market_id)
        .where(BookSnapshot.ts <= pos.opened_at)
        .order_by(desc(BookSnapshot.ts))
        .limit(1)
    )
    val = (await session.execute(stmt)).scalar_one_or_none()
    return float(val) if val is not None else None


# ---------------------------------------------------------------------------
# Decision per position
# ---------------------------------------------------------------------------


async def _update_peak(pos: PaperPosition, unrealized: float) -> None:
    """Mantiene high-water mark del unrealized PnL para el trailing stop."""
    current_peak = float(pos.peak_unrealized_pnl_usd)
    if unrealized > current_peak:
        pos.peak_unrealized_pnl_usd = Decimal(str(unrealized))


async def decide_exit_for(
    session: AsyncSession,
    pos: PaperPosition,
    portfolio_dd_pct: float,
) -> ExitDecision | None:
    now = datetime.now(UTC)
    age_h = (now - pos.opened_at).total_seconds() / 3600.0

    # T3: DD halt global se procesa en flatten_all aparte, pero respaldamos aquí.
    if portfolio_dd_pct <= -settings.dd_halt_pct:
        return ExitDecision(
            market_id=pos.market_id,
            side=pos.side,
            reason="t3_portfolio_dd_halt",
            fraction=1.0,
            mark_price_yes=0.5,
            pnl_pct_at_decision=0.0,
            age_hours=age_h,
        )

    # T1: outcome resuelto
    outcome = await _outcome_for(session, pos.market_id)
    if outcome is not None:
        # mark a 1 si lado coincide, 0 si no
        won = (
            (pos.side == "BUY_YES" and outcome.yes_outcome)
            or (pos.side == "BUY_NO" and not outcome.yes_outcome)
        )
        mark_yes = 1.0 if outcome.yes_outcome else 0.0
        pct = ((1.0 if won else 0.0) * float(pos.shares) - float(pos.total_cost_usd)) / max(
            float(pos.total_cost_usd), 1e-9
        )
        return ExitDecision(
            market_id=pos.market_id,
            side=pos.side,
            reason="t1_outcome_resolved",
            fraction=1.0,
            mark_price_yes=mark_yes,
            pnl_pct_at_decision=pct,
            age_hours=age_h,
        )

    # T0: stale book → recoger último mid si lo hay, salir
    mid_yes, observed_ts, snap = await _resolve_mid_yes(session, pos.market_id)
    if mid_yes is None:
        return None  # no podemos decidir sin precio
    if observed_ts is not None:
        if observed_ts.tzinfo is None:
            observed_ts = observed_ts.replace(tzinfo=UTC)
        if (now - observed_ts).total_seconds() > settings.stale_book_max_age_sec:
            unrealized, pct = _unrealized_pnl(pos, mid_yes)
            return ExitDecision(
                pos.market_id,
                pos.side,
                "t0_stale_book",
                1.0,
                mid_yes,
                pct,
                age_h,
            )

    unrealized, pnl_pct = _unrealized_pnl(pos, mid_yes)
    await _update_peak(pos, unrealized)

    # T2: pre-resolution
    end_date = await _market_end_date(session, pos.market_id)
    if end_date is not None:
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=UTC)
        hours_to_end = (end_date - now).total_seconds() / 3600.0
        if hours_to_end <= settings.exit_before_resolution_hours:
            return ExitDecision(
                pos.market_id, pos.side, "t2_pre_resolution", 1.0, mid_yes, pnl_pct, age_h
            )

    # T4: hard stop loss
    if pnl_pct <= -settings.stop_loss_pct:
        return ExitDecision(
            pos.market_id, pos.side, "t4_stop_loss", 1.0, mid_yes, pnl_pct, age_h
        )

    # T5: trailing stop
    peak_unreal = float(pos.peak_unrealized_pnl_usd)
    cost = max(float(pos.total_cost_usd), 1e-9)
    peak_pct = peak_unreal / cost
    if peak_pct >= settings.trailing_arm_pct:
        giveback_threshold = peak_unreal * (1 - settings.trailing_stop_giveback_pct)
        if unrealized <= giveback_threshold:
            return ExitDecision(
                pos.market_id,
                pos.side,
                "t5_trailing_stop",
                1.0,
                mid_yes,
                pnl_pct,
                age_h,
            )

    # T6: liquidity / spread degraded
    if snap is not None and snap.spread is not None:
        entry_spread = await _spread_at_entry(session, pos)
        cur_spread = float(snap.spread)
        if (
            entry_spread is not None
            and entry_spread > 0
            and cur_spread > entry_spread * settings.spread_blowout_multiplier
        ):
            return ExitDecision(
                pos.market_id,
                pos.side,
                "t6_liquidity_degraded",
                1.0,
                mid_yes,
                pnl_pct,
                age_h,
            )

    # T7: time stop
    if age_h >= settings.position_ttl_hours:
        return ExitDecision(
            pos.market_id, pos.side, "t7_time_stop", 1.0, mid_yes, pnl_pct, age_h
        )

    # T8: take profit
    if pnl_pct >= settings.take_profit_pct:
        return ExitDecision(
            pos.market_id, pos.side, "t8_take_profit", 1.0, mid_yes, pnl_pct, age_h
        )

    # T9: edge invalidation — re-corremos el edge y vemos si el side opuesto domina
    snapshots = await load_snapshots(session, pos.market_id, now)
    edge_now = detect_overreaction(snapshots, now)
    if edge_now is not None:
        # Si la "overreaction" actual sigue diciendo el MISMO lado, no es invalidación.
        # Pero si ahora el sigma apunta al lado opuesto con magnitud >= edge_invalidation_sigma:
        opposite = (
            (pos.side == "BUY_NO" and edge_now.side == "BUY_YES")
            or (pos.side == "BUY_YES" and edge_now.side == "BUY_NO")
        )
        if opposite and abs(edge_now.strength) >= settings.edge_invalidation_sigma:
            return ExitDecision(
                pos.market_id,
                pos.side,
                "t9_edge_invalidation",
                1.0,
                mid_yes,
                pnl_pct,
                age_h,
            )

    # T11/T12: gates técnicos (tendencia confirmada en contra, ruptura de S/R)
    ta_v = await evaluate_exit_ta(session, pos.market_id, pos.side)
    if ta_v.close:
        return ExitDecision(
            pos.market_id,
            pos.side,
            ta_v.reason or "ta_close",
            1.0,
            mid_yes,
            pnl_pct,
            age_h,
        )

    # T10: mean reversion target alcanzado → cerrar 50% (dejar runner)
    # Si el lado favorable se ha materializado (cruce de EMA), tomar parcial.
    if edge_now is None and snapshots:
        # Reconstruimos EMA simple sobre últimos N mids (igual que el edge):
        mids = [m for m in (edge_mid(s) for s in snapshots) if m is not None]
        if len(mids) >= settings.overreaction_min_snapshots:
            ema_val = edge_ema(mids[:-1], settings.ema_alpha)
            last = mids[-1]
            # BUY_NO se beneficia si el precio del lado YES baja hacia o bajo EMA
            # BUY_YES se beneficia si el precio del lado YES sube hacia o sobre EMA
            if pos.side == "BUY_NO" and last <= ema_val:
                return ExitDecision(
                    pos.market_id,
                    pos.side,
                    "t10_mean_revert_done",
                    0.5,
                    mid_yes,
                    pnl_pct,
                    age_h,
                )
            if pos.side == "BUY_YES" and last >= ema_val:
                return ExitDecision(
                    pos.market_id,
                    pos.side,
                    "t10_mean_revert_done",
                    0.5,
                    mid_yes,
                    pnl_pct,
                    age_h,
                )

    return None


# ---------------------------------------------------------------------------
# Execution loop
# ---------------------------------------------------------------------------


async def open_positions(session: AsyncSession) -> list[PaperPosition]:
    return (
        await session.execute(
            select(PaperPosition).where(
                PaperPosition.status == "open",
                PaperPosition.shares > 0,
            )
        )
    ).scalars().all()


async def _liquidity_for(session: AsyncSession, market_id: str) -> float | None:
    """Mejor estimación de liquidez ahora (para slippage de venta)."""
    stmt = (
        select(BookSnapshot.liquidity_num, BookSnapshot.volume_24hr)
        .where(BookSnapshot.market_id == market_id)
        .order_by(desc(BookSnapshot.ts))
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    liq, vol = row
    if liq is not None:
        return float(liq)
    return float(vol) if vol is not None else None


async def evaluate_and_execute_exits(
    session: AsyncSession,
    portfolio_dd_pct: float,
) -> list[ExitDecision]:
    """Pasada completa: evalúa todas las posiciones y ejecuta los cierres."""
    decisions: list[ExitDecision] = []
    positions = await open_positions(session)
    for pos in positions:
        d = await decide_exit_for(session, pos, portfolio_dd_pct)
        if d is None:
            continue
        liq = await _liquidity_for(session, pos.market_id)
        await execute_close(
            session=session,
            position=pos,
            current_mid_yes=d.mark_price_yes,
            liquidity_usd=liq,
            fraction=d.fraction,
            reason=d.reason,
            mode="sim",
            at_resolution=d.reason == "t1_outcome_resolved",
        )
        decisions.append(d)
    return decisions


async def flatten_all(
    session: AsyncSession, reason: str = "manual_flatten"
) -> int:
    """Cierra TODAS las posiciones abiertas al mid actual (o último conocido)."""
    n = 0
    positions = await open_positions(session)
    for pos in positions:
        mid_yes, _, _ = await _resolve_mid_yes(session, pos.market_id)
        if mid_yes is None:
            mid_yes = 0.5  # fallback simétrico — peor caso
        liq = await _liquidity_for(session, pos.market_id)
        await execute_close(
            session=session,
            position=pos,
            current_mid_yes=mid_yes,
            liquidity_usd=liq,
            fraction=1.0,
            reason=reason,
            mode="sim",
        )
        n += 1
    return n
