"""Motor de backtesting: replay deslizante sobre snapshots históricos.

Diseño (RESTRUCTURE_PLAN §8.1):
  - Para cada timestamp de evaluación (cada `step_minutes`), se filtran los
    snapshots con ts <= eval_ts (anti-lookahead estricto) y se corre el edge.
  - Si hay señal, se simula el fill con el MISMO modelo de slippage que el
    paper trading (`execution.paper.compute_fill_price`) para consistencia.
  - La posición se mantiene hasta la resolución del mercado y el PnL se calcula
    contra el outcome real (contrato binario: 1 USD/share si gana, 0 si pierde).
  - Un `cooldown_minutes` evita contar muchas señales correlacionadas del mismo
    mercado como trades independientes.
  - Cada mercado se evalúa solo dentro de su ventana de datos, con el mismo
    margen de frescura que aplica `risk.check` en vivo (`stale_book_max_age_sec`).
    Sin ese corte, el detector reemitía la misma señal sobre datos congelados
    hasta el `end` global.

Es lógica pura: recibe snapshots y outcomes ya cargados, sin DB ni red.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from umbra.backtest.metrics import MetricsReport, compute_metrics
from umbra.config import settings
from umbra.edges.overreaction import EdgeOutput
from umbra.engine.probability import compute_p_fair
from umbra.execution.paper import compute_fill_price
from umbra.features.calculator import SnapshotInput
from umbra.risk.sizer import size_position

DetectFn = Callable[[list[SnapshotInput], datetime], EdgeOutput | None]
PFairFn = Callable[[EdgeOutput], float]
# (side, p_fair_yes, market_price_yes) -> notional en USD. 0 = no operar.
SizerFn = Callable[[str, float, float], float]


def kelly_sizer(
    *,
    bankroll: float | None = None,
    kappa: float | None = None,
    max_risk_per_trade_usd: float | None = None,
) -> SizerFn:
    """Dimensiona como lo hace `orchestrator`: Kelly fraccional + cap por trade.

    Espeja el camino en vivo (`size_position` seguido del gate 8 del risk
    engine) para que las métricas midan la estrategia que realmente corre y no
    apuestas de tamaño fijo. Igual que en vivo, el bankroll es ESTÁTICO: no
    compone con el PnL acumulado.

    Lo que este sizer NO reproduce son los gates del risk engine que dependen
    de estado en base de datos —posición abierta, exposición bruta, reserva de
    caja, cooldown por fills, frescura del book—. Todos ellos solo pueden
    *reducir* o vetar un tamaño, nunca aumentarlo, así que el backtest queda
    optimista respecto a la ejecución real, no al revés. Conviene tenerlo
    presente al leer los resultados.
    """
    def _size(side: str, p_fair_yes: float, market_price_yes: float) -> float:
        sizing = size_position(
            side=side,
            p_fair_yes=p_fair_yes,
            market_price_yes=market_price_yes,
            bankroll=bankroll,
            kappa=kappa,
        )
        cap = (
            max_risk_per_trade_usd
            if max_risk_per_trade_usd is not None
            else settings.max_risk_per_trade_usd
        )
        return min(sizing.notional_usd, cap)

    return _size


@dataclass(frozen=True)
class BacktestTrade:
    market_id: str
    entry_ts: datetime
    side: str
    edge_name: str
    mid_yes: float
    fill_price: float
    p_fair_yes: float
    shares: float
    notional_usd: float
    outcome_yes: bool
    won: bool
    pnl_usd: float
    ret: float  # pnl / notional


@dataclass(frozen=True)
class BacktestResult:
    trades: list[BacktestTrade]
    metrics: MetricsReport


def _liquidity_at(snapshots: list[SnapshotInput]) -> float | None:
    for s in reversed(snapshots):
        if s.volume_24hr is not None:
            return float(s.volume_24hr)
    return None


def _won(side: str, outcome_yes: bool) -> bool:
    return (side == "BUY_YES" and outcome_yes) or (side == "BUY_NO" and not outcome_yes)


def _time_bounds(
    markets: dict[str, list[SnapshotInput]],
) -> tuple[datetime | None, datetime | None]:
    all_ts = [s.ts for snaps in markets.values() for s in snaps]
    return (min(all_ts), max(all_ts)) if all_ts else (None, None)


def run_backtest(
    markets: dict[str, list[SnapshotInput]],
    outcomes: dict[str, bool],
    detect_fn: DetectFn,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    step_minutes: int = 5,
    notional_usd: float = 10.0,
    cooldown_minutes: float = 60.0,
    p_fair_fn: PFairFn = compute_p_fair,
    initial_capital: float | None = None,
    sizer_fn: SizerFn | None = None,
) -> BacktestResult:
    """Corre el backtest sobre los mercados con outcome conocido.

    `markets`: {condition_id: [SnapshotInput, ...]} (no necesitan estar ordenados).
    `outcomes`: {condition_id: yes_outcome}. Mercados sin outcome se ignoran
    (no se puede calcular PnL sin resolución).
    `detect_fn`: típicamente `functools.partial(detect, sigma_threshold=..., ...)`.

    `sizer_fn`: cómo dimensionar cada trade. Por defecto None → `notional_usd`
    plano, que es útil para aislar la calidad del edge de la del sizing. Pásale
    `kelly_sizer()` para medir la estrategia tal y como corre en vivo; es lo que
    hace `scripts/run_backtest.py`. Con notional plano, métricas sensibles a la
    forma de la curva (max_drawdown, Sharpe) NO describen el sistema real.
    """
    auto_start, auto_end = _time_bounds(markets)
    start = start or auto_start
    end = end or auto_end
    capital = initial_capital if initial_capital is not None else settings.bankroll_usd
    trades: list[BacktestTrade] = []
    # Calibración: se registra CADA predicción del modelo, haya acabado o no en
    # trade. Puntuar el Brier solo sobre los trades lo sesga, porque el sizer
    # veta precisamente las señales de menor convicción: quedarían fuera del
    # cómputo justo las predicciones más flojas, y la calibración saldría mejor
    # de lo que es. La calidad de la estimación de probabilidad y la decisión de
    # operarla son dos cosas distintas y se miden por separado.
    predictions: list[float] = []
    baselines: list[float] = []
    pred_outcomes: list[int] = []

    if start is None or end is None:
        return BacktestResult(
            [], compute_metrics([], [], [], [], initial_capital=capital, baselines=[])
        )

    step = timedelta(minutes=step_minutes)
    cooldown = timedelta(minutes=cooldown_minutes)

    for cid, snaps in markets.items():
        if cid not in outcomes:
            continue
        if not snaps:
            continue
        outcome_yes = outcomes[cid]
        ordered = sorted(snaps, key=lambda s: s.ts)
        ts_index = [s.ts for s in ordered]

        # Cada mercado solo se evalúa dentro de SU ventana de datos, no en el
        # rango global. Dos motivos, y el primero es de correctitud:
        #
        # 1. Pasado el último snapshot, `visible` deja de cambiar y el detector
        #    vuelve a emitir la misma señal una vez por cooldown, indefinidamente.
        #    Un spike aislado producía 1 trade o 169 según lo lejos que llegara
        #    el `end` global, que lo fija el mercado más largo del histórico. Los
        #    mercados que resolvían pronto quedaban masivamente sobrerrepresentados.
        #    En vivo esto no ocurre: `risk.check` corta con `stale_book_max_age_sec`.
        #    Aquí se espeja ese mismo gate.
        # 2. Un mercado con datos de una hora dejaba de recorrer semanas de grid
        #    vacío.
        #
        # El grid se mantiene anclado a `start` global: los eval_ts posibles son
        # los mismos de antes, solo se recortan los que no podían producir nada
        # distinto. Los trades dentro de la ventana son idénticos.
        stale_allowance = timedelta(seconds=settings.stale_book_max_age_sec)
        market_end = min(end, ts_index[-1] + stale_allowance)
        if ts_index[0] > end or market_end < start:
            continue
        # Primer punto del grid global que ya ve al menos un snapshot.
        if ts_index[0] <= start:
            eval_ts = start
        else:
            n_steps = -(-(ts_index[0] - start) // step)  # ceil division
            eval_ts = start + n_steps * step

        last_entry: datetime | None = None
        while eval_ts <= market_end:
            if last_entry is not None and eval_ts - last_entry < cooldown:
                eval_ts += step
                continue
            # `visible` por puntero: `ordered` está ordenado, así que basta
            # localizar el corte en vez de refiltrar la lista entera en cada paso.
            visible = ordered[: bisect_right(ts_index, eval_ts)]
            edge = detect_fn(visible, eval_ts)
            if edge is not None:
                p_fair = p_fair_fn(edge)
                # Se registra antes de cualquier veto de sizing: esto mide al
                # modelo, no al gestor de riesgo.
                predictions.append(p_fair)
                pred_outcomes.append(1 if outcome_yes else 0)
                # La referencia contra la que se juzga la calibración: el precio
                # del mercado en ese mismo instante, sobre el mismo evento. Es lo
                # que convierte el criterio §14 en «¿aporta algo el modelo?» en vez
                # de «¿es bajo este número?». Ver `metrics.brier_skill`.
                baselines.append(float(edge.market_price))
                # El tamaño se decide ANTES del fill: el slippage de
                # `compute_fill_price` depende del notional, igual que en vivo.
                trade_notional = (
                    sizer_fn(edge.side, p_fair, edge.market_price)
                    if sizer_fn is not None
                    else notional_usd
                )
                if trade_notional <= 0:
                    # Kelly no ve apuesta. Espeja el gate 7 del risk engine
                    # ("kelly_zero_or_negative"): no hay trade, y tampoco
                    # cooldown, porque no se llegó a entrar.
                    eval_ts += step
                    continue
                liquidity = _liquidity_at(visible)
                # `compute_fill_price` trabaja en Decimal porque es el camino del
                # dinero real. Aquí es estadística: las métricas viven en float y
                # numpy. La frontera se cruza una vez, explícitamente.
                fill_price = float(
                    compute_fill_price(edge.side, edge.market_price, trade_notional, liquidity)[0]
                )
                shares = trade_notional / fill_price if fill_price > 0 else 0.0
                won = _won(edge.side, outcome_yes)
                pnl = shares * (1.0 if won else 0.0) - trade_notional
                trades.append(
                    BacktestTrade(
                        market_id=cid,
                        entry_ts=eval_ts,
                        side=edge.side,
                        edge_name=edge.edge_name,
                        mid_yes=edge.market_price,
                        fill_price=fill_price,
                        p_fair_yes=p_fair,
                        shares=shares,
                        notional_usd=trade_notional,
                        outcome_yes=outcome_yes,
                        won=won,
                        pnl_usd=pnl,
                        ret=pnl / trade_notional if trade_notional > 0 else 0.0,
                    )
                )
                last_entry = eval_ts
            eval_ts += step

    # Los trades se generan en bucle market-major (mercado externo, tiempo
    # interno), así que salen agrupados por mercado y no en orden temporal. La
    # equity acumulada de `compute_metrics` es una serie temporal: sobre el
    # orden de generación, el drawdown que reporta es el de una secuencia de
    # operaciones que nunca ocurrió.
    trades.sort(key=lambda t: (t.entry_ts, t.market_id))

    metrics = compute_metrics(
        pnls=[t.pnl_usd for t in trades],
        returns=[t.ret for t in trades],
        predictions=predictions,
        outcomes=pred_outcomes,
        initial_capital=capital,
        baselines=baselines,
    )
    return BacktestResult(trades=trades, metrics=metrics)
