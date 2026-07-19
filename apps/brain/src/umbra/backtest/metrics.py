"""Métricas de validación de un edge (puras, stdlib only).

Criterios de aceptación (ver RESTRUCTURE_PLAN §14):
  Brier < 0.20 · EV/señal > 0 · Profit Factor > 1.5 · Sharpe > 1.0 · MaxDD < 10%

Todas reciben listas de números o de `BacktestTrade` y no tocan estado global.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

# Umbral de Sharpe POR TRADE, no anualizado.
#
# El plan (§14) pedía "Sharpe > 1.0", cifra heredada de retornos diarios de
# renta variable. En contratos binarios es inalcanzable: el retorno por trade
# es bimodal y su std ronda 1.0, así que el ratio queda acotado muy por debajo.
# Simulando 2000 trades: un edge irreal de +15 puntos de probabilidad da 0.33;
# uno realista y fuerte de +4 puntos da 0.08. Un gate en 1.0 rechazaría
# siempre, incluido el bot perfecto.
#
# 0.10 ≈ el edge realista fuerte de esa simulación. Es un punto de partida
# defendible, NO un valor validado: recalíbralo contra trades reales en cuanto
# haya histórico, que hoy no lo hay.
SHARPE_PER_TRADE_MIN = 0.10


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

    mean / std muestral. Devuelve 0 si <2 trades o std=0.

    OJO con la escala: en contratos binarios el retorno por trade es bimodal
    (ganas +(1-p)/p, pierdes -100%), así que la std ronda 1.0 y este Sharpe se
    mueve en torno a 0.05-0.30 incluso para edges excelentes. NO es comparable
    con el Sharpe anualizado ~1.0 de renta variable. Ver `SHARPE_PER_TRADE_MIN`.
    """
    if len(returns) < 2:
        return 0.0
    mean = statistics.fmean(returns)
    # stdev (muestral, n-1) y no pstdev: estos retornos son una muestra de un
    # proceso, no la población. Con pocos trades pstdev subestima la dispersión
    # e infla el ratio justo cuando menos evidencia hay.
    std = statistics.stdev(returns)
    return mean / std if std > 0 else 0.0


def max_drawdown(pnls: list[float], initial_capital: float) -> float:
    """Max drawdown (fracción) sobre la curva de equity acumulada del backtest.

    La equity parte de `initial_capital` y suma el pnl de cada trade. Devuelve
    el peor giveback relativo al pico previo, como fracción positiva
    (0.10 = 10%).

    `initial_capital` es obligatorio y debe ser > 0: un drawdown *fraccional*
    no está definido sin una base de capital. La versión anterior asumía una
    equity que partía de 0, con lo que cualquier racha perdedora desde el
    inicio dejaba el pico en 0, se saltaba el cálculo y reportaba 0.0 — el
    caso más peligroso posible salía como el más limpio. Con capital inicial
    el pico nunca es 0 y la fórmula queda siempre definida.
    """
    if initial_capital <= 0:
        raise ValueError(
            f"initial_capital debe ser > 0 para un drawdown fraccional; got {initial_capital}"
        )
    equity = initial_capital
    peak = initial_capital
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
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
    # Predicciones puntuadas por el Brier. Puede ser > n_trades: el modelo emite
    # una probabilidad en cada detección, y el sizer luego decide si se opera o
    # no. Si diverge mucho de `n_trades`, el sizer está vetando buena parte de
    # las señales — merece la pena mirar por qué.
    n_predictions: int = 0

    def passes_acceptance(
        self,
        *,
        brier_max: float = 0.20,
        pf_min: float = 1.5,
        max_dd_max: float = 0.10,
        sharpe_min: float = SHARPE_PER_TRADE_MIN,
    ) -> bool:
        """Go/no-go según los umbrales objetivo del plan (§14).

        El Sharpe se comprueba aquí desde que existe el gate; antes figuraba en
        los criterios del docstring del módulo pero no se evaluaba.
        """
        if self.brier is None or self.brier >= brier_max:
            return False
        return (
            self.ev_per_signal_usd > 0
            and self.profit_factor >= pf_min
            and self.max_drawdown < max_dd_max
            and self.sharpe >= sharpe_min
        )


def compute_metrics(
    pnls: list[float],
    returns: list[float],
    predictions: list[float],
    outcomes: list[int],
    *,
    initial_capital: float,
) -> MetricsReport:
    """`initial_capital` es la base sobre la que se mide el drawdown fraccional.
    Pásale el bankroll con el que se corrió el backtest."""
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
        max_drawdown=max_drawdown(pnls, initial_capital),
        brier=brier_score(predictions, outcomes),
        n_predictions=len(predictions),
    )
