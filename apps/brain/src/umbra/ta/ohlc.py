"""OHLC aggregator — convierte book_snapshots en velas (candlesticks).

Diseño:
- intervalo configurable: '1m', '5m', '15m', '1h'.
- bucket alineado a UTC; el bucket de los últimos N segundos NO se persiste
  (está vivo), solo se persisten los buckets CERRADOS.
- precio = mid_yes = (best_bid + best_ask) / 2 (fallback last_trade_price).
- volume_proxy = avg(volume_24hr) dentro del bucket (Polymarket no devuelve
  volumen por tick — esto es solo un proxy visual).

Como un mercado puede tener snapshots irregulares (gaps), `n_snapshots` se
persiste por bar para que el dashboard pueda colorear barras de baja confianza.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.config import settings
from umbra.db.models import BookSnapshot, MarketActive, OhlcBar
from umbra.logging import get_logger

log = get_logger("umbra.ta.ohlc")


@dataclass(frozen=True)
class Bar:
    market_id: str
    interval: str
    bucket_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume_proxy: float | None
    n_snapshots: int


_INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
}


def interval_seconds(interval: str) -> int:
    if interval not in _INTERVAL_SECONDS:
        raise ValueError(f"interval desconocido: {interval}")
    return _INTERVAL_SECONDS[interval]


def bucket_start(ts: datetime, interval: str) -> datetime:
    """Trunca `ts` al inicio del bucket alineado a UTC."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    secs = interval_seconds(interval)
    unix = int(ts.timestamp())
    bucket_unix = unix - (unix % secs)
    return datetime.fromtimestamp(bucket_unix, tz=UTC)


def _snap_mid(snap: BookSnapshot) -> float | None:
    if snap.best_bid is not None and snap.best_ask is not None:
        return float((snap.best_bid + snap.best_ask) / 2)
    if snap.last_trade_price is not None:
        return float(snap.last_trade_price)
    return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


async def aggregate_bars(
    session: AsyncSession,
    market_id: str,
    interval: str,
    n_buckets_back: int = 120,
    include_open_bucket: bool = False,
) -> list[Bar]:
    """Construye velas OHLC para los últimos N buckets cerrados.

    Si `include_open_bucket=True` también incluye el bucket vivo (último, no cerrado).
    Útil para la UI; el job de persistencia debe usar False.
    """
    now = datetime.now(UTC)
    current_bucket = bucket_start(now, interval)
    secs = interval_seconds(interval)
    window_start = current_bucket - timedelta(seconds=secs * n_buckets_back)

    end_filter = (
        current_bucket + timedelta(seconds=secs)
        if include_open_bucket
        else current_bucket
    )

    snaps = (
        await session.execute(
            select(BookSnapshot)
            .where(
                BookSnapshot.market_id == market_id,
                BookSnapshot.ts >= window_start,
                BookSnapshot.ts < end_filter,
            )
            .order_by(BookSnapshot.ts.asc())
        )
    ).scalars().all()

    if not snaps:
        return []

    grouped: dict[datetime, list[BookSnapshot]] = {}
    for s in snaps:
        if s.ts.tzinfo is None:
            ts = s.ts.replace(tzinfo=UTC)
        else:
            ts = s.ts
        b = bucket_start(ts, interval)
        grouped.setdefault(b, []).append(s)

    bars: list[Bar] = []
    for b_ts in sorted(grouped.keys()):
        group = grouped[b_ts]
        mids = [m for m in (_snap_mid(s) for s in group) if m is not None]
        if not mids:
            continue
        vols = [
            float(s.volume_24hr) for s in group if s.volume_24hr is not None
        ]
        vol_proxy = (sum(vols) / len(vols)) if vols else None
        bars.append(
            Bar(
                market_id=market_id,
                interval=interval,
                bucket_start=b_ts,
                open=mids[0],
                high=max(mids),
                low=min(mids),
                close=mids[-1],
                volume_proxy=vol_proxy,
                n_snapshots=len(group),
            )
        )
    return bars


# ---------------------------------------------------------------------------
# Persist (upsert on conflict)
# ---------------------------------------------------------------------------


async def persist_bars(session: AsyncSession, bars: list[Bar]) -> int:
    n = 0
    for b in bars:
        stmt = (
            pg_insert(OhlcBar)
            .values(
                market_id=b.market_id,
                interval=b.interval,
                bucket_start=b.bucket_start,
                open_price=Decimal(str(b.open)),
                high_price=Decimal(str(b.high)),
                low_price=Decimal(str(b.low)),
                close_price=Decimal(str(b.close)),
                volume_proxy=(
                    Decimal(str(b.volume_proxy)) if b.volume_proxy is not None else None
                ),
                n_snapshots=b.n_snapshots,
            )
            .on_conflict_do_update(
                constraint="uq_ohlc_market_interval_bucket",
                set_={
                    "open_price": Decimal(str(b.open)),
                    "high_price": Decimal(str(b.high)),
                    "low_price": Decimal(str(b.low)),
                    "close_price": Decimal(str(b.close)),
                    "volume_proxy": (
                        Decimal(str(b.volume_proxy))
                        if b.volume_proxy is not None
                        else None
                    ),
                    "n_snapshots": b.n_snapshots,
                },
            )
        )
        await session.execute(stmt)
        n += 1
    return n


async def aggregate_and_persist_universe(session: AsyncSession) -> int:
    """Para todos los mercados del universo activo, persiste OHLC de cada intervalo."""
    cids = (
        await session.execute(select(MarketActive.condition_id))
    ).scalars().all()
    total = 0
    for cid in cids:
        for interval in settings.ohlc_intervals:
            bars = await aggregate_bars(
                session,
                cid,
                interval,
                n_buckets_back=settings.ohlc_lookback_bars,
                include_open_bucket=False,
            )
            total += await persist_bars(session, bars)
    return total


# ---------------------------------------------------------------------------
# Read for TA / dashboard
# ---------------------------------------------------------------------------


async def read_bars(
    session: AsyncSession,
    market_id: str,
    interval: str,
    n: int = 120,
) -> list[Bar]:
    """Devuelve los últimos N bars persistidos + el bucket vivo (live)."""
    rows = (
        await session.execute(
            select(OhlcBar)
            .where(OhlcBar.market_id == market_id, OhlcBar.interval == interval)
            .order_by(OhlcBar.bucket_start.desc())
            .limit(n)
        )
    ).scalars().all()
    closed = [
        Bar(
            market_id=r.market_id,
            interval=r.interval,
            bucket_start=r.bucket_start,
            open=float(r.open_price),
            high=float(r.high_price),
            low=float(r.low_price),
            close=float(r.close_price),
            volume_proxy=float(r.volume_proxy) if r.volume_proxy is not None else None,
            n_snapshots=r.n_snapshots,
        )
        for r in rows
    ]
    closed.reverse()  # ascending
    # Añadir bucket vivo si tenemos snapshots desde el último bar
    live_bars = await aggregate_bars(
        session,
        market_id,
        interval,
        n_buckets_back=1,
        include_open_bucket=True,
    )
    if live_bars:
        last_live = live_bars[-1]
        # Si ya estaba en closed, lo reemplazamos. Si es nuevo, lo añadimos.
        if closed and closed[-1].bucket_start == last_live.bucket_start:
            closed[-1] = last_live
        else:
            closed.append(last_live)
    return closed
