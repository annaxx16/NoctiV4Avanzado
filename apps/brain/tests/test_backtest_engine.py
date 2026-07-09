"""Tests del motor de backtesting (puro, sin DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial

from umbra.backtest.engine import run_backtest
from umbra.edges.overreaction import detect as detect_overreaction
from umbra.features.calculator import SnapshotInput

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

# Ruido pequeño para que recent_std > 0 sin enmascarar el spike.
_NOISE = [0.0, 0.001, -0.001, 0.002, -0.002, 0.001, -0.001, 0.0, 0.002, -0.001,
          0.001, -0.002, 0.0, 0.001, -0.001]


def _snap(minute: int, price: float) -> SnapshotInput:
    return SnapshotInput(
        ts=BASE + timedelta(minutes=minute),
        best_bid=price - 0.005,
        best_ask=price + 0.005,
        last_trade_price=price,
        spread=0.01,
        volume_24hr=50_000.0,
    )


def _market_with_spike_up() -> list[SnapshotInput]:
    """15 puntos estables ~0.30, luego un spike a 0.45 (overreaction al alza)."""
    snaps = [_snap(i, 0.30 + _NOISE[i]) for i in range(15)]
    snaps.append(_snap(15, 0.45))
    return snaps


def test_backtest_detecta_spike_y_buy_no_gana():
    markets = {"0xtest_spike": _market_with_spike_up()}
    # Outcome NO (yes_outcome=False): BUY_NO debe ganar → PnL positivo.
    outcomes = {"0xtest_spike": False}

    res = run_backtest(
        markets,
        outcomes,
        partial(detect_overreaction, sigma_threshold=3.0),
        step_minutes=1,
        cooldown_minutes=120.0,
        notional_usd=10.0,
    )

    assert res.metrics.n_trades >= 1
    t = res.trades[0]
    assert t.side == "BUY_NO"
    assert t.won is True
    assert t.pnl_usd > 0
    # cooldown alto → un único trade pese a que la señal persiste varios ticks
    assert res.metrics.n_trades == 1


def test_backtest_buy_no_pierde_si_resuelve_yes():
    markets = {"0xtest_spike": _market_with_spike_up()}
    outcomes = {"0xtest_spike": True}  # YES gana → BUY_NO pierde todo
    res = run_backtest(
        markets,
        outcomes,
        partial(detect_overreaction, sigma_threshold=3.0),
        step_minutes=1,
        cooldown_minutes=120.0,
    )
    assert res.metrics.n_trades == 1
    assert res.trades[0].won is False
    assert res.trades[0].pnl_usd == -10.0  # pérdida total del notional


def test_backtest_sin_outcome_se_ignora():
    markets = {"0xtest_spike": _market_with_spike_up()}
    res = run_backtest(markets, {}, detect_overreaction, step_minutes=1)
    assert res.metrics.n_trades == 0


def test_backtest_sin_señal_si_estable():
    # Mercado plano: nunca supera el umbral de sigma.
    flat = {"0xtest_flat": [_snap(i, 0.30 + _NOISE[i]) for i in range(15)]}
    res = run_backtest(
        flat, {"0xtest_flat": True},
        partial(detect_overreaction, sigma_threshold=3.0),
        step_minutes=1,
    )
    assert res.metrics.n_trades == 0
