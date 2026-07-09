"""Fase 0 — investigación de régimen y drawdown sobre el histórico.

OBSERVACIONAL: no decide trades, solo mide si hay regímenes detectables y cómo
quiebra cada serie. Carga los snapshots reales de la DB; si no hay suficientes,
cae a una serie sintética con regímenes conocidos para que el prototipo corra ya.

Uso:
    python scripts/run_regime_research.py                 # DB o fallback sintético
    python scripts/run_regime_research.py --synthetic     # fuerza sintético
    python scripts/run_regime_research.py --since-days 30 --window-min 30 --k 4
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from umbra.research.drawdown import summarize_drawdown
from umbra.research.regime import cluster_regimes, extract_regime_features
from umbra.research.series import PricePoint, snapshots_to_series
from umbra.research.synthetic import generate_series

MIN_POINTS = 50  # por debajo de esto la DB no da para clusterizar; usar sintético


async def _load_real(since_days: int) -> dict[str, list[PricePoint]]:
    """Carga series reales por mercado. Import perezoso: si no hay DB/driver,
    el fallback sintético sigue funcionando."""
    from umbra.backtest.loader import load_backtest_data
    from umbra.db.session import dispose, get_sessionmaker

    since = datetime.now(UTC) - timedelta(days=since_days)
    sm = get_sessionmaker()
    async with sm() as session:
        markets, _ = await load_backtest_data(session, since=since)
    await dispose()
    return {mid: snapshots_to_series(snaps) for mid, snaps in markets.items()}


def _analyze(name: str, points: list[PricePoint], window_min: int, k: int) -> None:
    print(f"\n=== Serie: {name} | {len(points)} puntos ===")
    if len(points) < MIN_POINTS:
        print(f"  (insuficiente: <{MIN_POINTS} puntos)")
        return

    dd = summarize_drawdown(points)
    print("-- Drawdown --")
    print(f"  episodios={dd.n_episodes} maxDD={dd.max_drawdown:.1%} "
          f"profundidad_media={dd.mean_depth:.1%} sin_recuperar={dd.n_unrecovered}")
    print(f"  fraccion_underwater={dd.fraction_underwater:.1%}")
    if dd.worst_episode is not None:
        w = dd.worst_episode
        rec = "sí" if w.recovered else "NO"
        print(f"  peor: -{w.depth:.1%} en {w.time_to_trough_s/3600:.1f}h (recuperó: {rec})")

    window = timedelta(minutes=window_min)
    step = max(1, len(points) // 200)  # ~200 evaluaciones máx
    feats = []
    for i in range(0, len(points), step):
        f = extract_regime_features(points, points[i].ts, window=window)
        if f is not None:
            feats.append(f)

    if len(feats) < k:
        print("-- Régimen -- (muy pocos vectores para clusterizar)")
        return

    _, profiles = cluster_regimes(feats, k=k)
    print(f"-- Régimen (k={k}, {len(feats)} vectores) --")
    for p in sorted(profiles, key=lambda x: -x.size):
        share = p.size / len(feats)
        print(f"  [{p.name:8}] {share:5.1%}  vol={p.mean_volatility:.4f} "
              f"|drift|={p.mean_abs_drift:.3f} dd={p.mean_drawdown_depth:.1%}")


async def main(args: argparse.Namespace) -> None:
    series: dict[str, list[PricePoint]] = {}

    if not args.synthetic:
        try:
            series = await _load_real(args.since_days)
        except Exception as exc:  # noqa: BLE001 — prototipo: degradar a sintético
            print(f"(sin DB / error de carga: {exc}) → fallback sintético")

    total = sum(len(v) for v in series.values())
    if total < MIN_POINTS:
        if series:
            print(f"Solo {total} puntos reales → fallback sintético.")
        pts, truth = generate_series()
        n_cascade = sum(1 for t in truth if t == "CASCADE")
        print(f"Serie sintética: {len(pts)} puntos, {n_cascade} en CASCADE (ground-truth).")
        _analyze("synthetic", pts, args.window_min, args.k)
        return

    print(f"Mercados reales: {len(series)} | puntos totales: {total}")
    for mid, pts in sorted(series.items(), key=lambda kv: -len(kv[1]))[: args.top]:
        _analyze(mid, pts, args.window_min, args.k)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", help="forzar serie sintética")
    ap.add_argument("--since-days", type=int, default=30)
    ap.add_argument("--window-min", type=int, default=30, help="ventana trailing del régimen")
    ap.add_argument("--k", type=int, default=4, help="número de regímenes a buscar")
    ap.add_argument("--top", type=int, default=5, help="mercados (por nº de puntos) a analizar")
    asyncio.run(main(ap.parse_args()))
