"""Portfolio Manager v2 — equity REAL, drawdown, gross exposure.

Cambios vs v1:
- `portfolio_snapshot` ahora incluye realized_pnl_total, gross_exposure, peak_equity,
  drawdown_pct (sobre la curva persistida en equity_snapshots).
- `equity_curve` lee de la tabla EquitySnapshot (mark-to-market real), no cost-basis.
- `persist_equity_snapshot` lo invoca el background loop cada N segundos.
- mark-to-market sigue usando mid_yes con asimetría (1-mid_yes) para BUY_NO —
  refinable cuando tengamos best_bid del lado NO desde el CLOB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.cache.book_cache import get_book
from umbra.config import settings
from umbra.db.models import (
    BookSnapshot,
    EquitySnapshot,
    Outcome,
    PaperPosition,
)


@dataclass(frozen=True)
class PositionView:
    market_id: str
    side: str
    shares: float
    avg_entry_price: float
    current_price: float | None
    current_value_usd: float | None
    unrealized_pnl_usd: float | None
    unrealized_pnl_pct: float | None
    realized_pnl_usd: float
    peak_unrealized_pnl_usd: float
    total_cost_usd: float
    n_fills: int
    age_hours: float
    opened_at: datetime
    last_updated_at: datetime
    status: str


@dataclass(frozen=True)
class PortfolioSnapshot:
    ts: datetime
    cash_usd: float
    positions_value_usd: float
    equity_usd: float
    unrealized_pnl_usd: float
    realized_pnl_usd_total: float
    gross_exposure_usd: float
    peak_equity_usd: float
    drawdown_pct: float
    total_cost_usd: float
    n_open_positions: int


# ---------------------------------------------------------------------------
# Mark-to-market sources
# ---------------------------------------------------------------------------


def _side_current_price(side: str, mid_yes: float) -> float:
    return mid_yes if side == "BUY_YES" else (1 - mid_yes)


def _mid_from_snapshot(snap: BookSnapshot) -> float | None:
    if snap.best_bid is not None and snap.best_ask is not None:
        return float((snap.best_bid + snap.best_ask) / 2)
    if snap.last_trade_price is not None:
        return float(snap.last_trade_price)
    return None


async def _bulk_outcomes(
    session: AsyncSession, market_ids: list[str]
) -> dict[str, bool]:
    """{market_id: yes_outcome} para los mercados resueltos del set. 1 query."""
    if not market_ids:
        return {}
    rows = (
        await session.execute(
            select(Outcome.market_id, Outcome.yes_outcome).where(
                Outcome.market_id.in_(market_ids)
            )
        )
    ).all()
    return dict(rows)


async def _bulk_latest_books(
    session: AsyncSession, market_ids: list[str]
) -> dict[str, BookSnapshot]:
    """Último BookSnapshot por mercado en 1 query (DISTINCT ON de Postgres)."""
    if not market_ids:
        return {}
    stmt = (
        select(BookSnapshot)
        .where(BookSnapshot.market_id.in_(market_ids))
        .distinct(BookSnapshot.market_id)
        .order_by(BookSnapshot.market_id, desc(BookSnapshot.ts))
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {snap.market_id: snap for snap in rows}


# ---------------------------------------------------------------------------
# Position views
# ---------------------------------------------------------------------------


async def position_views(
    session: AsyncSession, include_closed: bool = False
) -> list[PositionView]:
    stmt = select(PaperPosition)
    if not include_closed:
        stmt = stmt.where(PaperPosition.status == "open", PaperPosition.shares > 0)
    rows = (await session.execute(stmt)).scalars().all()

    # Bulk-prefetch: 2 queries para todo el set en vez de ~2 por posición.
    market_ids = list({p.market_id for p in rows})
    outcomes_map = await _bulk_outcomes(session, market_ids)
    books_map = await _bulk_latest_books(session, market_ids)

    async def _mid_for(market_id: str) -> float | None:
        # Prioridad: outcome resuelto (0/1) > cache Redis (más fresco) > DB.
        if market_id in outcomes_map:
            return 1.0 if outcomes_map[market_id] else 0.0
        cached = await get_book(market_id)
        if cached is not None and cached.best_bid is not None and cached.best_ask is not None:
            return (cached.best_bid + cached.best_ask) / 2.0
        if cached is not None and cached.last_trade_price is not None:
            return cached.last_trade_price
        snap = books_map.get(market_id)
        return _mid_from_snapshot(snap) if snap is not None else None

    now = datetime.now(UTC)
    views: list[PositionView] = []
    for p in rows:
        mid = await _mid_for(p.market_id)
        cur_price = _side_current_price(p.side, mid) if mid is not None else None
        cur_value = float(p.shares) * cur_price if cur_price is not None else None
        cost = float(p.total_cost_usd)
        pnl = (cur_value - cost) if cur_value is not None else None
        pct = (pnl / cost) if (pnl is not None and cost > 0) else None
        opened_at = p.opened_at if p.opened_at.tzinfo else p.opened_at.replace(tzinfo=UTC)
        age_h = (now - opened_at).total_seconds() / 3600.0
        views.append(
            PositionView(
                market_id=p.market_id,
                side=p.side,
                shares=float(p.shares),
                avg_entry_price=float(p.avg_entry_price),
                current_price=cur_price,
                current_value_usd=cur_value,
                unrealized_pnl_usd=pnl,
                unrealized_pnl_pct=pct,
                realized_pnl_usd=float(p.realized_pnl_usd),
                peak_unrealized_pnl_usd=float(p.peak_unrealized_pnl_usd),
                total_cost_usd=cost,
                n_fills=p.n_fills,
                age_hours=age_h,
                opened_at=opened_at,
                last_updated_at=p.last_updated_at,
                status=p.status,
            )
        )
    return views


# ---------------------------------------------------------------------------
# Portfolio aggregates
# ---------------------------------------------------------------------------


async def _realized_total(session: AsyncSession) -> float:
    val = (
        await session.execute(
            select(func.coalesce(func.sum(PaperPosition.realized_pnl_usd), 0))
        )
    ).scalar()
    return float(val or 0)


async def _latest_flat_snapshot_ts(session: AsyncSession) -> datetime | None:
    """Ultimo punto sin posiciones; separa ciclos de riesgo activo."""
    return (
        await session.execute(
            select(EquitySnapshot.ts)
            .where(EquitySnapshot.n_open_positions == 0)
            .order_by(desc(EquitySnapshot.ts))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _peak_equity(session: AsyncSession, since_ts: datetime | None = None) -> float:
    stmt = select(func.coalesce(func.max(EquitySnapshot.equity_usd), 0))
    if since_ts is not None:
        stmt = stmt.where(EquitySnapshot.ts >= since_ts)
    val = (await session.execute(stmt)).scalar()
    return float(val or 0)


async def portfolio_snapshot(session: AsyncSession) -> PortfolioSnapshot:
    views = await position_views(session, include_closed=False)
    gross_exposure = sum(v.total_cost_usd for v in views)
    positions_value = sum(
        (v.current_value_usd if v.current_value_usd is not None else v.total_cost_usd)
        for v in views
    )
    unrealized = sum((v.unrealized_pnl_usd or 0.0) for v in views)
    realized_total = await _realized_total(session)
    cash = settings.bankroll_usd + realized_total - gross_exposure
    equity = cash + positions_value

    if views:
        cycle_start_ts = await _latest_flat_snapshot_ts(session)
        peak_hist = await _peak_equity(session, since_ts=cycle_start_ts)
        peak = max(peak_hist, equity)
    else:
        # Sin posiciones abiertas empieza un nuevo ciclo de riesgo. Un pico
        # historico anterior no debe dejar el bot en DD halt para siempre.
        peak = equity
    drawdown_pct = (equity - peak) / peak if peak > 0 else 0.0

    return PortfolioSnapshot(
        ts=datetime.now(UTC),
        cash_usd=cash,
        positions_value_usd=positions_value,
        equity_usd=equity,
        unrealized_pnl_usd=unrealized,
        realized_pnl_usd_total=realized_total,
        gross_exposure_usd=gross_exposure,
        peak_equity_usd=peak,
        drawdown_pct=drawdown_pct,
        total_cost_usd=gross_exposure,
        n_open_positions=len(views),
    )


# ---------------------------------------------------------------------------
# Equity curve (REAL, no cost-basis)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EquityPoint:
    ts: datetime
    equity_usd: float
    cash_usd: float
    positions_value_usd: float
    unrealized_pnl_usd: float
    realized_pnl_usd_total: float
    gross_exposure_usd: float
    peak_equity_usd: float
    drawdown_pct: float
    n_open_positions: int


async def equity_curve(
    session: AsyncSession,
    lookback_hours: int = 24,
) -> list[EquityPoint]:
    since = datetime.now(UTC) - timedelta(hours=lookback_hours)
    rows = (
        await session.execute(
            select(EquitySnapshot)
            .where(EquitySnapshot.ts >= since)
            .order_by(EquitySnapshot.ts.asc())
        )
    ).scalars().all()

    return [
        EquityPoint(
            ts=r.ts,
            equity_usd=float(r.equity_usd),
            cash_usd=float(r.cash_usd),
            positions_value_usd=float(r.positions_value_usd),
            unrealized_pnl_usd=float(r.unrealized_pnl_usd),
            realized_pnl_usd_total=float(r.realized_pnl_usd_total),
            gross_exposure_usd=float(r.gross_exposure_usd),
            peak_equity_usd=float(r.peak_equity_usd),
            drawdown_pct=float(r.drawdown_pct),
            n_open_positions=r.n_open_positions,
        )
        for r in rows
    ]


async def persist_equity_snapshot(session: AsyncSession) -> EquitySnapshot:
    snap = await portfolio_snapshot(session)
    row = EquitySnapshot(
        ts=snap.ts,
        cash_usd=Decimal(str(snap.cash_usd)),
        positions_value_usd=Decimal(str(snap.positions_value_usd)),
        equity_usd=Decimal(str(snap.equity_usd)),
        unrealized_pnl_usd=Decimal(str(snap.unrealized_pnl_usd)),
        realized_pnl_usd_total=Decimal(str(snap.realized_pnl_usd_total)),
        gross_exposure_usd=Decimal(str(snap.gross_exposure_usd)),
        peak_equity_usd=Decimal(str(snap.peak_equity_usd)),
        drawdown_pct=Decimal(str(snap.drawdown_pct)),
        n_open_positions=snap.n_open_positions,
    )
    session.add(row)
    await session.flush()
    return row
