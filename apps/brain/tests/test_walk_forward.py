"""Tests del walk-forward y la calibración de hiperparámetros (puro)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from umbra.backtest.walk_forward import calibrate, walk_forward
from umbra.features.calculator import SnapshotInput

BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
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


def _market_spiking_at(offset_min: int) -> list[SnapshotInput]:
    snaps = [_snap(offset_min + i, 0.30 + _NOISE[i]) for i in range(15)]
    snaps.append(_snap(offset_min + 15, 0.45))  # spike up → BUY_NO
    return snaps


def _dataset():
    # 6 mercados con spikes escalonados a lo largo de la línea temporal.
    markets = {f"0xtest_wf_{k}": _market_spiking_at(k * 30) for k in range(6)}
    outcomes = {cid: False for cid in markets}  # BUY_NO gana en todos
    return markets, outcomes


def test_calibrate_elige_un_sigma_del_grid():
    markets, outcomes = _dataset()
    cal = calibrate(markets, outcomes, min_trades=1, step_minutes=1, cooldown_minutes=500)
    assert cal is not None
    assert cal.best_sigma in (2.5, 3.0, 3.5, 4.0)
    assert cal.best_ema_alpha in (0.05, 0.10, 0.15)
    assert cal.metrics.ev_per_signal_usd > 0  # todos los BUY_NO ganan


def test_walk_forward_produce_splits():
    markets, outcomes = _dataset()
    results = walk_forward(
        markets, outcomes,
        n_splits=2, train_pct=0.6,
        min_trades=1, step_minutes=1, cooldown_minutes=500,
    )
    assert len(results) >= 1
    split = results[0]
    assert split.best_sigma in (2.5, 3.0, 3.5, 4.0)
    assert split.train_ev > 0
    assert isinstance(split.degradation, float)


def test_walk_forward_vacio_sin_datos():
    assert walk_forward({}, {}) == []
