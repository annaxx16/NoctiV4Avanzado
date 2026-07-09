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

Es lógica pura: recibe snapshots y outcomes ya cargados, sin DB ni red.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from umbra.backtest.metrics import MetricsReport, compute_metrics
from umbra.edges.overreaction import EdgeOutput
from umbra.engine.probability import compute_p_fair
from umbra.execution.paper import compute_fill_price
from umbra.features.calculator import SnapshotInput

DetectFn = Callable[[list[SnapshotInput], datetime], EdgeOutput | None]
PFairFn = Callable[[EdgeOutput], float]


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
) -> BacktestResult:
    """Corre el backtest sobre los mercados con outcome conocido.

    `markets`: {condition_id: [SnapshotInput, ...]} (no necesitan estar ordenados).
    `outcomes`: {condition_id: yes_outcome}. Mercados sin outcome se ignoran
    (no se puede calcular PnL sin resolución).
    `detect_fn`: típicamente `functools.partial(detect, sigma_threshold=..., ...)`.
    """
    auto_start, auto_end = _time_bounds(markets)
    start = start or auto_start
    end = end or auto_end
    trades: list[BacktestTrade] = []

    if start is None or end is None:
        return BacktestResult([], compute_metrics([], [], [], []))

    step = timedelta(minutes=step_minutes)
    cooldown = timedelta(minutes=cooldown_minutes)

    for cid, snaps in markets.items():
        if cid not in outcomes:
            continue
        outcome_yes = outcomes[cid]
        ordered = sorted(snaps, key=lambda s: s.ts)

        last_entry: datetime | None = None
        eval_ts = start
        while eval_ts <= end:
            if last_entry is not None and eval_ts - last_entry < cooldown:
                eval_ts += step
                continue
            visible = [s for s in ordered if s.ts <= eval_ts]
            edge = detect_fn(visible, eval_ts)
            if edge is not None:
                liquidity = _liquidity_at(visible)
                fill_price, _ = compute_fill_price(
                    edge.side, edge.market_price, notional_usd, liquidity
                )
                shares = notional_usd / fill_price if fill_price > 0 else 0.0
                won = _won(edge.side, outcome_yes)
                pnl = shares * (1.0 if won else 0.0) - notional_usd
                trades.append(
                    BacktestTrade(
                        market_id=cid,
                        entry_ts=eval_ts,
                        side=edge.side,
                        edge_name=edge.edge_name,
                        mid_yes=edge.market_price,
                        fill_price=fill_price,
                        p_fair_yes=p_fair_fn(edge),
                        shares=shares,
                        notional_usd=notional_usd,
                        outcome_yes=outcome_yes,
                        won=won,
                        pnl_usd=pnl,
                        ret=pnl / notional_usd if notional_usd > 0 else 0.0,
                    )
                )
                last_entry = eval_ts
            eval_ts += step

    metrics = compute_metrics(
        pnls=[t.pnl_usd for t in trades],
        returns=[t.ret for t in trades],
        predictions=[t.p_fair_yes for t in trades],
        outcomes=[1 if t.outcome_yes else 0 for t in trades],
    )
    return BacktestResult(trades=trades, metrics=metrics)
