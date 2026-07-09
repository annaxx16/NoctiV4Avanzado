"""Walk-forward analysis y calibración de hiperparámetros (RESTRUCTURE_PLAN §8.2).

Filosofía: calibrar `sigma_threshold` en una ventana de train y evaluarlo en la
ventana de test inmediatamente posterior (out-of-sample, SIN reoptimizar). Si la
degradación de EV train→test supera un umbral, el edge no está validado.

Las ventanas acotan únicamente los timestamps de EVALUACIÓN (`start`/`end`); el
historial para la EMA sigue siendo completo hasta cada eval_ts, de modo que las
señales de test no pierden contexto al cruzar la frontera train/test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial

from umbra.backtest.engine import BacktestResult, run_backtest
from umbra.backtest.metrics import MetricsReport
from umbra.edges.overreaction import detect as detect_overreaction
from umbra.features.calculator import SnapshotInput

DEFAULT_SIGMA_GRID = (2.5, 3.0, 3.5, 4.0)
DEFAULT_EMA_GRID = (0.05, 0.10, 0.15)


@dataclass(frozen=True)
class CalibrationResult:
    best_sigma: float
    best_ema_alpha: float
    metrics: MetricsReport


@dataclass(frozen=True)
class WalkForwardSplit:
    period: str
    best_sigma: float
    best_ema_alpha: float
    train_ev: float
    test_ev: float
    test_brier: float | None
    test_sharpe: float
    degradation: float  # (train_ev - test_ev) / |train_ev|; <0 = mejora en test


def calibrate(
    markets: dict[str, list[SnapshotInput]],
    outcomes: dict[str, bool],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    sigma_grid: tuple[float, ...] = DEFAULT_SIGMA_GRID,
    ema_grid: tuple[float, ...] = DEFAULT_EMA_GRID,
    min_trades: int = 3,
    step_minutes: int = 5,
    notional_usd: float = 10.0,
    cooldown_minutes: float = 60.0,
) -> CalibrationResult | None:
    """Barrido en grid (sigma × ema_alpha) maximizando EV por señal.

    Solo considera combinaciones con al menos `min_trades` trades (evita elegir
    un threshold con 1 trade afortunado). Devuelve None si nada califica.
    """
    best: CalibrationResult | None = None
    for sigma in sigma_grid:
        for alpha in ema_grid:
            detect_fn = partial(
                detect_overreaction, sigma_threshold=sigma, ema_alpha=alpha
            )
            res = run_backtest(
                markets,
                outcomes,
                detect_fn,
                start=start,
                end=end,
                step_minutes=step_minutes,
                notional_usd=notional_usd,
                cooldown_minutes=cooldown_minutes,
            )
            if res.metrics.n_trades < min_trades:
                continue
            if best is None or res.metrics.ev_per_signal_usd > best.metrics.ev_per_signal_usd:
                best = CalibrationResult(sigma, alpha, res.metrics)
    return best


def _evaluate(
    markets: dict[str, list[SnapshotInput]],
    outcomes: dict[str, bool],
    sigma: float,
    alpha: float,
    *,
    start: datetime,
    end: datetime,
    step_minutes: int,
    notional_usd: float,
    cooldown_minutes: float,
) -> BacktestResult:
    return run_backtest(
        markets,
        outcomes,
        partial(detect_overreaction, sigma_threshold=sigma, ema_alpha=alpha),
        start=start,
        end=end,
        step_minutes=step_minutes,
        notional_usd=notional_usd,
        cooldown_minutes=cooldown_minutes,
    )


def walk_forward(
    markets: dict[str, list[SnapshotInput]],
    outcomes: dict[str, bool],
    *,
    n_splits: int = 5,
    train_pct: float = 0.6,
    sigma_grid: tuple[float, ...] = DEFAULT_SIGMA_GRID,
    ema_grid: tuple[float, ...] = DEFAULT_EMA_GRID,
    min_trades: int = 3,
    step_minutes: int = 5,
    notional_usd: float = 10.0,
    cooldown_minutes: float = 60.0,
) -> list[WalkForwardSplit]:
    """Divide la línea temporal en `n_splits` ventanas. En cada una calibra en
    train y evalúa en el test posterior. Devuelve un resultado por split válido.
    """
    all_ts = [s.ts for snaps in markets.values() for s in snaps]
    if not all_ts or n_splits < 2:
        return []
    min_ts, max_ts = min(all_ts), max(all_ts)
    total = max_ts - min_ts
    if total <= timedelta(0):
        return []
    window = total / n_splits

    results: list[WalkForwardSplit] = []
    for i in range(n_splits - 1):
        train_start = min_ts
        train_end = min_ts + window * (i + 1)
        test_start = train_end
        test_end = test_start + window * (1 - train_pct)
        if test_end <= test_start:
            continue

        cal = calibrate(
            markets,
            outcomes,
            start=train_start,
            end=train_end,
            sigma_grid=sigma_grid,
            ema_grid=ema_grid,
            min_trades=min_trades,
            step_minutes=step_minutes,
            notional_usd=notional_usd,
            cooldown_minutes=cooldown_minutes,
        )
        if cal is None:
            continue

        test = _evaluate(
            markets,
            outcomes,
            cal.best_sigma,
            cal.best_ema_alpha,
            start=test_start,
            end=test_end,
            step_minutes=step_minutes,
            notional_usd=notional_usd,
            cooldown_minutes=cooldown_minutes,
        )
        train_ev = cal.metrics.ev_per_signal_usd
        test_ev = test.metrics.ev_per_signal_usd
        degradation = (
            (train_ev - test_ev) / abs(train_ev) if train_ev != 0 else 0.0
        )
        results.append(
            WalkForwardSplit(
                period=f"{test_start.date()} – {test_end.date()}",
                best_sigma=cal.best_sigma,
                best_ema_alpha=cal.best_ema_alpha,
                train_ev=train_ev,
                test_ev=test_ev,
                test_brier=test.metrics.brier,
                test_sharpe=test.metrics.sharpe,
                degradation=degradation,
            )
        )
    return results
