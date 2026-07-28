"""Métricas de validación de un edge (puras, stdlib only).

Criterios de aceptación (§14):
  bate al mercado con significancia · EV/señal > 0 · Profit Factor > 1.5
  · Sharpe/trade >= 0.10 · MaxDD < 10%

El criterio de calibración es RELATIVO. «Brier < 0.20» lo cumple un sistema sin
edge: el 28/07/2026, sobre 1.799 eventos reales, el modelo dio 0.0723 y el precio
de mercado 0.0722. Los dos aprobaban, y la diferencia era cero. Ver
`brier_skill` y `MetricsReport.passes_acceptance`.

Todas reciben listas de números o de `BacktestTrade` y no tocan estado global.
"""

from __future__ import annotations

import math
import random
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


@dataclass(frozen=True)
class BrierSkill:
    """Cuánto mejor predice el modelo que la referencia, y si es real.

    `diff_mean` es la diferencia media de errores cuadráticos, modelo menos
    referencia: **negativo = el modelo predice mejor**. `ci_lo`/`ci_hi` son el
    intervalo del 95% por bootstrap emparejado.

    `beats_baseline` solo es cierto si el intervalo entero queda por debajo de
    cero. Un punto estimado favorable con el intervalo cruzando el cero es ruido,
    y este dataclass existe precisamente para que no se pueda confundir con
    evidencia.
    """

    n: int
    brier_model: float
    brier_baseline: float
    diff_mean: float
    ci_lo: float
    ci_hi: float

    @property
    def beats_baseline(self) -> bool:
        return self.ci_hi < 0.0


def brier_skill(
    predictions: list[float],
    baselines: list[float],
    outcomes: list[int],
    *,
    n_boot: int = 10_000,
    seed: int = 7,
) -> BrierSkill | None:
    """Compara el Brier del modelo contra el de una referencia, sobre los MISMOS
    eventos.

    La referencia natural aquí es el precio de mercado. Un Brier absoluto bajo no
    dice nada por sí solo: si la mayoría de mercados resuelven cerca de su precio,
    cualquiera saca 0.07 copiando el precio. La pregunta útil no es "¿es bajo?"
    sino "¿es más bajo que el del mercado, y por más de lo que explica el azar?".

    Bootstrap emparejado: se remuestrean los pares (error_modelo, error_mercado)
    del mismo evento, así que la correlación entre ambos —que es altísima, porque
    predicen lo mismo— no infla el intervalo. `seed` fijo: un gate go/no-go no
    puede dar un veredicto distinto en dos ejecuciones sobre los mismos datos.
    """
    if not predictions or len(predictions) != len(outcomes) != len(baselines):
        return None
    if len(baselines) != len(predictions):
        return None

    pares = [
        ((p - o) ** 2, (b - o) ** 2)
        for p, b, o in zip(predictions, baselines, outcomes, strict=True)
    ]
    n = len(pares)
    diffs = [em - eb for em, eb in pares]
    media = statistics.fmean(diffs)

    rng = random.Random(seed)
    medias = sorted(
        statistics.fmean([diffs[rng.randrange(n)] for _ in range(n)])
        for _ in range(n_boot)
    )
    lo = medias[int(0.025 * n_boot)]
    hi = medias[int(0.975 * n_boot)]

    return BrierSkill(
        n=n,
        brier_model=statistics.fmean([em for em, _ in pares]),
        brier_baseline=statistics.fmean([eb for _, eb in pares]),
        diff_mean=media,
        ci_lo=lo,
        ci_hi=hi,
    )


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
    # Modelo contra precio de mercado sobre los mismos eventos. `None` si el
    # backtest no aportó referencia — y entonces el gate no puede pasar.
    skill: BrierSkill | None = None

    def passes_acceptance(
        self,
        *,
        pf_min: float = 1.5,
        max_dd_max: float = 0.10,
        sharpe_min: float = SHARPE_PER_TRADE_MIN,
        min_predictions: int = 200,
    ) -> bool:
        """Go/no-go según los criterios §14.

        EL GATE DE CALIBRACIÓN ES RELATIVO, NO ABSOLUTO
        -----------------------------------------------
        Antes exigía `brier < 0.20`. Ese umbral **lo cumple un sistema sin edge
        alguno**. Medido sobre datos reales el 28/07/2026: el modelo sacó 0.0723 y
        el precio de mercado 0.0722 sobre los mismos 1.799 eventos. Ambos aprueban
        holgadamente un gate de 0.20, y la diferencia entre ellos era cero.

        La razón es aritmética: si la mayoría de mercados resuelven cerca de su
        precio, copiar el precio ya da un Brier bajo. Un umbral absoluto no premia
        acertar, premia operar en mercados fáciles.

        Así que ahora se exige **batir al mercado con significancia**: el intervalo
        de confianza del 95% de la diferencia emparejada tiene que quedar entero
        por debajo de cero. Sin referencia de mercado (`skill is None`) no se puede
        afirmar nada y el gate no pasa — fail-closed, como el resto del sistema.

        `min_predictions` existe porque con muestra pequeña el intervalo es tan
        ancho que nunca excluirá el cero; declararlo evita leer un no-pasa por
        falta de datos como un no-pasa por falta de edge.
        """
        if self.skill is None or not self.skill.beats_baseline:
            return False
        if self.skill.n < min_predictions:
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
    baselines: list[float] | None = None,
) -> MetricsReport:
    """`initial_capital` es la base sobre la que se mide el drawdown fraccional.
    Pásale el bankroll con el que se corrió el backtest.

    `baselines` son las probabilidades de la referencia —el precio de mercado en
    el momento de cada predicción— sobre los mismos eventos y en el mismo orden.
    Sin ellas no hay `skill`, y sin `skill` el gate §14 no puede pasar. Es
    opcional por compatibilidad con los llamantes que aún no la pasan, no porque
    sea prescindible.
    """
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
        skill=(
            None
            if baselines is None
            else brier_skill(predictions, baselines, outcomes)
        ),
    )
