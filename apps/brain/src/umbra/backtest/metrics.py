"""Métricas de validación de un edge (puras, stdlib only).

Criterios de aceptación (ver RESTRUCTURE_PLAN §14):
  Brier < 0.20 · EV/señal > 0 · Profit Factor > 1.5 · Sharpe > 1.0 · MaxDD < 10%

Todas reciben listas de números o de `BacktestTrade` y no tocan estado global.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


def brier_score(predictions: list[float], outcomes: list[int]) -> float | None:
    """Brier = media de (p - o)². `p` ∈ [0,1] (P(YES)), `o` ∈ {0,1}.

    Mide calibración: 0 = perfecto, 0.25 = baseline (predecir siempre 0.5),
    1 = peor caso. Devuelve None si no hay datos.
    """
    if not predictions or len(predictions) != len(outcomes):
        return None
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes, strict=False)) / len(predictions)


def hit_rate(wins: int, total: int) -> float:
    return wins / total if total > 0 else 0.0


def ev_per_signal(pnls: list[float]) -> float:
    """EV por señal = PnL medio por trade (en USD)."""
    return statistics.fmean(pnls) if pnls else 0.0


def profit_factor(pnls: list[float]) -> float:
    """Σ ganancias / |Σ pérdidas|. inf si no hay pérdidas y sí ganancias."""
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses == 0:
        return math.inf if gains > 0 else 0.0
    return gains / losses


def sharpe(returns: list[float]) -> float:
    """Sharpe NO anualizado sobre retornos por trade (pnl/notional).

    mean / std. Devuelve 0 si <2 trades o std=0.
    """
    if len(returns) < 2:
        return 0.0
    mean = statistics.fmean(returns)
    std = statistics.pstdev(returns)
    return mean / std if std > 0 else 0.0


def max_drawdown(pnls: list[float]) -> float:
    """Max drawdown (fracción) sobre la curva de equity acumulada del backtest.

    Equity parte de 0 y suma pnl por trade. Devuelve el peor giveback relativo
    al pico previo, como fracción positiva (0.10 = 10%). Sin pico positivo → 0.
    """
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        if peak > 0:
            dd = (peak - equity) / peak
            max_dd = max(max_dd, dd)
    return max_dd


@dataclass(frozen=True)
class MetricsReport:
    n_trades: int
    n_wins: int
    hit_rate: float
    total_pnl_usd: float
    ev_per_signal_usd: float
    profit_factor: float
    sharpe: float
    max_drawdown: float
    brier: float | None

    def passes_acceptance(
        self,
        *,
        brier_max: float = 0.20,
        pf_min: float = 1.5,
        max_dd_max: float = 0.10,
    ) -> bool:
        """Go/no-go según los umbrales objetivo del plan (§14)."""
        if self.brier is None or self.brier >= brier_max:
            return False
        return (
            self.ev_per_signal_usd > 0
            and self.profit_factor >= pf_min
            and self.max_drawdown < max_dd_max
        )


def compute_metrics(
    pnls: list[float],
    returns: list[float],
    predictions: list[float],
    outcomes: list[int],
) -> MetricsReport:
    wins = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    return MetricsReport(
        n_trades=n,
        n_wins=wins,
        hit_rate=hit_rate(wins, n),
        total_pnl_usd=sum(pnls),
        ev_per_signal_usd=ev_per_signal(pnls),
        profit_factor=profit_factor(pnls),
        sharpe=sharpe(returns),
        max_drawdown=max_drawdown(pnls),
        brier=brier_score(predictions, outcomes),
    )
