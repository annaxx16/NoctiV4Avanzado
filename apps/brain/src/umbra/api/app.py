from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra import __version__
from umbra.cache.book_cache import get_book as cache_get_book
from umbra.cache.redis_client import dispose as redis_dispose
from umbra.cache.redis_client import ping as redis_ping
from umbra.config import settings
from umbra.db.models import BookSnapshot, Market, MarketActive, PaperFill, Signal
from umbra.db.session import dispose as db_dispose
from umbra.db.session import get_session
from umbra.engine.exit_engine import flatten_all
from umbra.features.calculator import calculate_features
from umbra.features.loader import load_snapshots
from umbra.logging import configure_logging, get_logger
from umbra.portfolio.manager import equity_curve, portfolio_snapshot, position_views
from umbra.risk.engine import halt_reason, is_halted, set_halt
from umbra.scheduler.background import BackgroundTasks
from umbra.ta.levels import classify_levels
from umbra.ta.ohlc import read_bars
from umbra.ta.trend import analyze_trend


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    log = get_logger("umbra.api")
    log.info("api.startup", version=__version__, mode=settings.mode)

    bg = BackgroundTasks()
    await bg.start()
    app.state.bg = bg
    try:
        yield
    finally:
        await bg.stop()
        await redis_dispose()
        await db_dispose()
        log.info("api.shutdown")


app = FastAPI(title="umbraNocti", version=__version__, lifespan=lifespan)


async def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Gate fail-closed para /admin/*: exige X-Admin-Token == settings.admin_token.

    Si `admin_token` no está configurado, se rechaza todo (503): preferimos
    inutilizar el panel admin antes que dejarlo abierto.
    """
    if not settings.admin_token:
        raise HTTPException(status_code=503, detail="admin disabled: set ADMIN_TOKEN")
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=403, detail="invalid admin token")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/version")
async def version() -> dict[str, str]:
    return {"version": __version__, "mode": settings.mode}


@app.get("/universe")
async def universe(
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    stmt = (
        select(MarketActive, Market.question, Market.slug)
        .join(Market, Market.condition_id == MarketActive.condition_id)
        .order_by(MarketActive.rank)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "rank": ma.rank,
            "condition_id": ma.condition_id,
            "slug": slug,
            "question": question,
            "liquidity_num": float(ma.liquidity_num) if ma.liquidity_num else None,
            "volume_24hr": float(ma.volume_24hr) if ma.volume_24hr else None,
        }
        for ma, question, slug in rows
    ]


@app.get("/snapshots/{condition_id}")
async def snapshots(
    condition_id: str,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be in [1, 1000]")
    stmt = (
        select(BookSnapshot)
        .where(BookSnapshot.market_id == condition_id)
        .order_by(desc(BookSnapshot.ts))
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "ts": row.ts.isoformat(),
            "best_bid": float(row.best_bid) if row.best_bid is not None else None,
            "best_ask": float(row.best_ask) if row.best_ask is not None else None,
            "last_trade_price": float(row.last_trade_price)
            if row.last_trade_price is not None
            else None,
            "spread": float(row.spread) if row.spread is not None else None,
        }
        for row in rows
    ]


@app.get("/markets/{condition_id}/features")
async def market_features(
    condition_id: str,
    as_of: datetime | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    as_of_resolved = as_of or datetime.now(UTC)
    if as_of_resolved.tzinfo is None:
        as_of_resolved = as_of_resolved.replace(tzinfo=UTC)

    market = (
        await session.execute(select(Market).where(Market.condition_id == condition_id))
    ).scalar_one_or_none()
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")

    snapshots = await load_snapshots(session, condition_id, as_of_resolved)
    fs = calculate_features(snapshots, as_of_resolved)
    return {
        "condition_id": condition_id,
        "slug": market.slug,
        "features": fs.as_dict(),
    }


@app.get("/markets/{condition_id}/book")
async def market_book(condition_id: str) -> dict[str, Any]:
    cached = await cache_get_book(condition_id)
    if cached is None:
        raise HTTPException(status_code=404, detail="no fresh book in cache")
    return {
        "condition_id": cached.condition_id,
        "ts": cached.ts,
        "best_bid": cached.best_bid,
        "best_ask": cached.best_ask,
        "last_trade_price": cached.last_trade_price,
        "spread": cached.spread,
        "liquidity_num": cached.liquidity_num,
        "volume_24hr": cached.volume_24hr,
    }


@app.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    from sqlalchemy import func as sa_func

    market_count = (await session.execute(select(sa_func.count(Market.condition_id)))).scalar()
    snapshot_count = (
        await session.execute(select(sa_func.count(BookSnapshot.id)))
    ).scalar()
    universe_size = (
        await session.execute(select(sa_func.count(MarketActive.condition_id)))
    ).scalar()
    signal_count = (await session.execute(select(sa_func.count(Signal.id)))).scalar()
    accepted_count = (
        await session.execute(
            select(sa_func.count(Signal.id)).where(Signal.accepted.is_(True))
        )
    ).scalar()
    return {
        "markets": market_count,
        "snapshots": snapshot_count,
        "universe_size": universe_size,
        "signals_total": signal_count,
        "signals_accepted": accepted_count,
    }


@app.get("/signals")
async def signals(
    limit: int = 20,
    accepted_only: bool = False,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be in [1, 500]")
    stmt = select(Signal).order_by(desc(Signal.ts)).limit(limit)
    if accepted_only:
        stmt = stmt.where(Signal.accepted.is_(True))
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": s.id,
            "ts": s.ts.isoformat(),
            "market_id": s.market_id,
            "edge": s.edge_name,
            "side": s.side,
            "market_price": float(s.market_price),
            "fair_price": float(s.fair_price),
            "edge_value": float(s.edge_value),
            "strength": float(s.strength) if s.strength is not None else None,
            "size_shares": float(s.size_shares) if s.size_shares is not None else None,
            "notional_usd": float(s.notional_usd) if s.notional_usd is not None else None,
            "accepted": s.accepted,
            "reason": s.reason,
            "mode": s.mode,
        }
        for s in rows
    ]


@app.get("/portfolio")
async def portfolio(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    snap = await portfolio_snapshot(session)
    views = await position_views(session)
    return {
        "ts": snap.ts.isoformat(),
        "cash_usd": snap.cash_usd,
        "positions_value_usd": snap.positions_value_usd,
        "equity_usd": snap.equity_usd,
        "unrealized_pnl_usd": snap.unrealized_pnl_usd,
        "realized_pnl_usd_total": snap.realized_pnl_usd_total,
        "gross_exposure_usd": snap.gross_exposure_usd,
        "peak_equity_usd": snap.peak_equity_usd,
        "drawdown_pct": snap.drawdown_pct,
        "total_cost_usd": snap.total_cost_usd,
        "n_open_positions": snap.n_open_positions,
        "positions": [
            {
                "market_id": v.market_id,
                "side": v.side,
                "shares": v.shares,
                "avg_entry_price": v.avg_entry_price,
                "current_price": v.current_price,
                "current_value_usd": v.current_value_usd,
                "unrealized_pnl_usd": v.unrealized_pnl_usd,
                "unrealized_pnl_pct": v.unrealized_pnl_pct,
                "realized_pnl_usd": v.realized_pnl_usd,
                "peak_unrealized_pnl_usd": v.peak_unrealized_pnl_usd,
                "total_cost_usd": v.total_cost_usd,
                "n_fills": v.n_fills,
                "age_hours": v.age_hours,
                "status": v.status,
                "opened_at": v.opened_at.isoformat(),
                "last_updated_at": v.last_updated_at.isoformat(),
            }
            for v in views
        ],
    }


@app.get("/portfolio/health")
async def portfolio_health(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    snap = await portfolio_snapshot(session)
    halted = await is_halted()
    redis_ok = await redis_ping()
    active_halt_reason = await halt_reason() if halted else None
    dd_throttle = snap.drawdown_pct <= -settings.dd_throttle_pct
    dd_halt = snap.drawdown_pct <= -settings.dd_halt_pct
    return {
        "ts": snap.ts.isoformat(),
        "equity_usd": snap.equity_usd,
        "peak_equity_usd": snap.peak_equity_usd,
        "drawdown_pct": snap.drawdown_pct,
        "gross_exposure_usd": snap.gross_exposure_usd,
        "cash_usd": snap.cash_usd,
        "n_open_positions": snap.n_open_positions,
        "redis_ok": redis_ok,
        "halted": halted,
        "halt_reason": active_halt_reason,
        "halted_by_redis_failure": halted and not redis_ok,
        "halted_by_kill_switch": halted and redis_ok,
        "circuit_breakers": {
            "dd_throttle_active": dd_throttle,
            "dd_halt_active": dd_halt,
            "dd_throttle_pct": settings.dd_throttle_pct,
            "dd_halt_pct": settings.dd_halt_pct,
        },
    }


@app.get("/portfolio/equity-curve")
async def portfolio_equity_curve(
    hours: int = 24,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    if hours < 1 or hours > 24 * 7:
        raise HTTPException(status_code=400, detail="hours must be in [1, 168]")
    points = await equity_curve(session, lookback_hours=hours)
    return [
        {
            "ts": p.ts.isoformat(),
            "equity_usd": p.equity_usd,
            "cash_usd": p.cash_usd,
            "positions_value_usd": p.positions_value_usd,
            "unrealized_pnl_usd": p.unrealized_pnl_usd,
            "realized_pnl_usd_total": p.realized_pnl_usd_total,
            "gross_exposure_usd": p.gross_exposure_usd,
            "peak_equity_usd": p.peak_equity_usd,
            "drawdown_pct": p.drawdown_pct,
            "n_open_positions": p.n_open_positions,
        }
        for p in points
    ]


@app.get("/markets/{condition_id}/candles")
async def market_candles(
    condition_id: str,
    interval: str = "5m",
    n: int = 120,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Velas OHLC + tendencia + soportes/resistencias para un mercado."""
    if interval not in settings.ohlc_intervals:
        raise HTTPException(
            status_code=400,
            detail=f"interval must be one of {settings.ohlc_intervals}",
        )
    if n < 5 or n > 500:
        raise HTTPException(status_code=400, detail="n must be in [5, 500]")

    bars = await read_bars(session, condition_id, interval, n=n)
    if not bars:
        return {
            "condition_id": condition_id,
            "interval": interval,
            "bars": [],
            "trend": None,
            "levels": None,
        }

    trend = analyze_trend(
        bars,
        ema_fast_period=settings.ta_ema_fast,
        ema_slow_period=settings.ta_ema_slow,
    )
    levels = classify_levels(
        bars,
        window=3,
        tolerance=0.015,
        min_touches=settings.ta_sr_min_touches,
    )
    return {
        "condition_id": condition_id,
        "interval": interval,
        "bars": [
            {
                "ts": b.bucket_start.isoformat(),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume_proxy": b.volume_proxy,
                "n_snapshots": b.n_snapshots,
            }
            for b in bars
        ],
        "trend": {
            "regime": trend.regime,
            "ema_fast": trend.ema_fast,
            "ema_slow": trend.ema_slow,
            "slope": trend.slope,
            "channel_high": trend.channel_high,
            "channel_low": trend.channel_low,
            "channel_mid": trend.channel_mid,
            "channel_width_pct": trend.channel_width_pct,
            "position_in_channel": trend.position_in_channel,
        },
        "levels": {
            "last_close": levels.last_close,
            "supports": [
                {"price": lv.price, "touches": lv.touches} for lv in levels.supports[:5]
            ],
            "resistances": [
                {"price": lv.price, "touches": lv.touches} for lv in levels.resistances[:5]
            ],
            "nearest_support": (
                {"price": levels.nearest_support.price, "touches": levels.nearest_support.touches}
                if levels.nearest_support
                else None
            ),
            "nearest_resistance": (
                {
                    "price": levels.nearest_resistance.price,
                    "touches": levels.nearest_resistance.touches,
                }
                if levels.nearest_resistance
                else None
            ),
        },
    }


@app.get("/exits")
async def exits(
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Últimos fills de cierre (action='CLOSE'), con su realized PnL."""
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be in [1, 500]")
    rows = (
        await session.execute(
            select(PaperFill)
            .where(PaperFill.action == "CLOSE")
            .order_by(desc(PaperFill.ts))
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": f.id,
            "ts": f.ts.isoformat(),
            "market_id": f.market_id,
            "side": f.side,
            "shares": float(f.shares),
            "mid_at_fill": float(f.mid_at_fill),
            "fill_price": float(f.fill_price),
            "slippage_bps": float(f.slippage_bps),
            "proceeds_usd": float(f.notional_usd),
            "realized_pnl_usd": float(f.realized_pnl_usd),
            "mode": f.mode,
        }
        for f in rows
    ]


@app.get("/fills")
async def fills(
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be in [1, 500]")
    rows = (
        await session.execute(
            select(PaperFill).order_by(desc(PaperFill.ts)).limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": f.id,
            "ts": f.ts.isoformat(),
            "signal_id": f.signal_id,
            "market_id": f.market_id,
            "side": f.side,
            "shares": float(f.shares),
            "mid_at_fill": float(f.mid_at_fill),
            "fill_price": float(f.fill_price),
            "slippage_bps": float(f.slippage_bps),
            "notional_usd": float(f.notional_usd),
            "fees_usd": float(f.fees_usd),
            "mode": f.mode,
        }
        for f in rows
    ]


@app.post("/admin/halt")
async def admin_halt(
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Halt = bloquea nuevas señales Y cierra TODAS las posiciones abiertas."""
    await set_halt(True, reason="manual")
    n = await flatten_all(session, reason="admin_halt")
    await session.commit()
    return {"status": "halted", "flattened": n}


@app.post("/admin/resume")
async def admin_resume(_: None = Depends(require_admin)) -> dict[str, str]:
    await set_halt(False)
    return {"status": "resumed"}


@app.post("/admin/flatten")
async def admin_flatten(
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    """Flatten manual sin activar halt — útil para pruebas."""
    n = await flatten_all(session, reason="admin_flatten")
    await session.commit()
    return {"status": "flattened", "n": n}
