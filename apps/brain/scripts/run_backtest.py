"""Backtest offline de OverreactionV1 sobre los snapshots ya persistidos.

Carga book_snapshots + outcomes desde la DB, corre el backtest con los
parámetros de producción, hace análisis de sensibilidad (grid sigma × ema) y
walk-forward, e imprime un veredicto go/no-go contra los criterios del §14.

Uso:
    python scripts/run_backtest.py
    python scripts/run_backtest.py --since-days 30 --step 5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from functools import partial

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from umbra.backtest.engine import run_backtest
from umbra.backtest.loader import load_backtest_data
from umbra.backtest.walk_forward import calibrate, walk_forward
from umbra.db.session import dispose, get_sessionmaker
from umbra.edges.overreaction import detect as detect_overreaction


async def main(since_days: int, step: int) -> None:
    since = datetime.now(UTC) - timedelta(days=since_days)
    sm = get_sessionmaker()
    async with sm() as session:
        markets, outcomes = await load_backtest_data(session, since=since)

    n_resolved = len(outcomes)
    n_snaps = sum(len(v) for v in markets.values())
    print(f"Mercados: {len(markets)} | snapshots: {n_snaps} | resueltos: {n_resolved}")
    if n_resolved == 0:
        print("Sin outcomes resueltos todavía → no se puede validar PnL/Brier.")
        await dispose()
        return

    base = run_backtest(
        markets, outcomes, partial(detect_overreaction), step_minutes=step
    )
    m = base.metrics
    print("\n== Parámetros de producción ==")
    print(f"trades={m.n_trades} hit={m.hit_rate:.1%} EV/señal=${m.ev_per_signal_usd:.4f}")
    print(f"PF={m.profit_factor:.2f} Sharpe={m.sharpe:.2f} MaxDD={m.max_drawdown:.1%} "
          f"Brier={m.brier if m.brier is None else round(m.brier, 4)}")

    print("\n== Calibración (grid sigma × ema) ==")
    cal = calibrate(markets, outcomes, step_minutes=step)
    if cal is not None:
        print(f"mejor sigma={cal.best_sigma} ema={cal.best_ema_alpha} "
              f"EV/señal=${cal.metrics.ev_per_signal_usd:.4f}")

    print("\n== Walk-forward ==")
    for s in walk_forward(markets, outcomes, step_minutes=step):
        print(f"{s.period}: sigma={s.best_sigma} ema={s.best_ema_alpha} "
              f"train_EV=${s.train_ev:.4f} test_EV=${s.test_ev:.4f} "
              f"degr={s.degradation:.1%} Brier={s.test_brier}")

    print("\n== Veredicto (§14) ==")
    print("APROBADO ✅" if m.passes_acceptance() else "NO APROBADO ❌ — seguir en paper")

    await dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-days", type=int, default=30)
    ap.add_argument("--step", type=int, default=5)
    args = ap.parse_args()
    asyncio.run(main(args.since_days, args.step))
