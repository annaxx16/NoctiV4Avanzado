"""Health check rápido del estado del bot — sin arrancar la API.

Conecta a Postgres + Redis y devuelve:
- estado del kill switch
- portfolio snapshot (equity, DD, posiciones)
- counts de tablas clave
- migración aplicada en la DB

Uso:
    python scripts/health_check.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import func, select, text  # noqa: E402

from umbra.cache.redis_client import dispose as redis_dispose  # noqa: E402
from umbra.cache.redis_client import get_redis
from umbra.db.models import (  # noqa: E402
    BookSnapshot,
    EquitySnapshot,
    Market,
    MarketActive,
    OhlcBar,
    Outcome,
    PaperFill,
    PaperPosition,
    Signal,
)
from umbra.db.session import dispose as db_dispose  # noqa: E402
from umbra.db.session import get_sessionmaker
from umbra.portfolio.manager import portfolio_snapshot  # noqa: E402
from umbra.risk.engine import KILL_SWITCH_KEY  # noqa: E402


def line(label: str, value) -> None:
    print(f"  {label:.<40} {value}")


async def main() -> int:
    print("\n=== umbraNocti health check ===\n")

    # 1. Migración
    sm = get_sessionmaker()
    async with sm() as session:
        rev = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
        print("Migración:")
        line("alembic version_num", rev)

    # 2. Redis / kill switch
    print("\nRedis:")
    redis = get_redis()
    try:
        halt_val = await redis.get(KILL_SWITCH_KEY)
        line("kill_switch_active", "YES" if halt_val == "1" else "NO")
        line("redis ping", "OK" if await redis.ping() else "FAIL")
    except Exception as exc:
        line("redis", f"ERROR: {exc}")

    # 3. Counts de tablas
    print("\nTablas (counts):")
    async with sm() as session:
        for model, label in [
            (Market, "markets"),
            (MarketActive, "markets_active"),
            (BookSnapshot, "book_snapshots"),
            (Signal, "signals"),
            (PaperFill, "fills_paper"),
            (PaperPosition, "portfolio_state"),
            (Outcome, "outcomes"),
            (EquitySnapshot, "equity_snapshots"),
            (OhlcBar, "ohlc_bars"),
        ]:
            n = (
                await session.execute(select(func.count()).select_from(model))
            ).scalar()
            line(label, n)

    # 4. Portfolio snapshot (debe ser equity=bankroll, DD=0 si reset OK)
    print("\nPortfolio snapshot:")
    async with sm() as session:
        snap = await portfolio_snapshot(session)
        line("equity_usd", f"${snap.equity_usd:.2f}")
        line("cash_usd", f"${snap.cash_usd:.2f}")
        line("positions_value_usd", f"${snap.positions_value_usd:.2f}")
        line("unrealized_pnl_usd", f"${snap.unrealized_pnl_usd:.2f}")
        line("realized_pnl_usd_total", f"${snap.realized_pnl_usd_total:.2f}")
        line("gross_exposure_usd", f"${snap.gross_exposure_usd:.2f}")
        line("peak_equity_usd", f"${snap.peak_equity_usd:.2f}")
        line("drawdown_pct", f"{snap.drawdown_pct * 100:.4f}%")
        line("n_open_positions", snap.n_open_positions)

    # 5. Posiciones abiertas (deberían ser 0 tras reset)
    print("\nPosiciones abiertas (debe estar vacío tras reset):")
    async with sm() as session:
        open_rows = (
            await session.execute(
                select(PaperPosition).where(PaperPosition.status == "open")
            )
        ).scalars().all()
        if open_rows:
            for p in open_rows:
                line(
                    f"{p.market_id[:24]}.. ({p.side})",
                    f"shares={float(p.shares):.2f} cost=${float(p.total_cost_usd):.2f}",
                )
        else:
            line("(ninguna)", "OK")

    await redis_dispose()
    await db_dispose()
    print("\n=== health check OK ===\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
