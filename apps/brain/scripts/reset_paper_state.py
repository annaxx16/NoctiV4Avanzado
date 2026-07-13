"""Reset del estado de paper trading.

Borra: signals, fills, portfolio_state, equity_snapshots.
Mantiene: markets, book_snapshots, markets_active, outcomes, ohlc_bars
          (para no perder histórico de datos ni velas ya agregadas).

También limpia el kill-switch en Redis por si quedó activo de la sesión anterior.

Uso:
    python scripts/reset_paper_state.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Permitir ejecutar desde la raíz del repo
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import text  # noqa: E402

from umbra.cache.redis_client import dispose as redis_dispose  # noqa: E402
from umbra.cache.redis_client import get_redis
from umbra.db.session import dispose as db_dispose  # noqa: E402
from umbra.db.session import get_sessionmaker
from umbra.risk.engine import KILL_SWITCH_KEY  # noqa: E402


async def main() -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        await session.execute(
            text(
                "TRUNCATE fills, portfolio_state, equity_snapshots, signals "
                "RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
    print("DB reset: fills, portfolio_state, equity_snapshots, signals truncated.")

    redis = get_redis()
    try:
        await redis.delete(KILL_SWITCH_KEY)
        print(f"Redis: {KILL_SWITCH_KEY} cleared.")
    except Exception as exc:
        print(f"Redis cleanup skipped: {exc}")

    await redis_dispose()
    await db_dispose()
    print("Reset done.")


if __name__ == "__main__":
    asyncio.run(main())
