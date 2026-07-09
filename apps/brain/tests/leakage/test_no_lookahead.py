"""Tests obligatorios anti-lookahead.

Verifican que ningún cálculo de features usa datos con ts > as_of.
Si fallan: el modelo va a "predecir" usando el futuro y los backtests serán mentira.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from umbra.features.calculator import (
    SnapshotInput,
    calculate_features,
)


def _snap(seconds_ago: int, bid: float, ask: float, vol_24h: float = 1000.0):
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    return SnapshotInput(
        ts=base - timedelta(seconds=seconds_ago),
        best_bid=bid,
        best_ask=ask,
        last_trade_price=(bid + ask) / 2,
        spread=ask - bid,
        volume_24hr=vol_24h,
    )


BASE_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_future_snapshots_are_ignored():
    """Si llega un snapshot con ts > as_of, el calculator debe ignorarlo."""
    past = SnapshotInput(
        ts=BASE_TS - timedelta(minutes=1),
        best_bid=0.40,
        best_ask=0.42,
        last_trade_price=0.41,
        spread=0.02,
        volume_24hr=1000.0,
    )
    future = SnapshotInput(
        ts=BASE_TS + timedelta(minutes=5),  # FUTURO
        best_bid=0.99,
        best_ask=0.99,
        last_trade_price=0.99,
        spread=0.00,
        volume_24hr=999999.0,
    )

    fs_with_future = calculate_features([past, future], as_of=BASE_TS)
    fs_without_future = calculate_features([past], as_of=BASE_TS)

    assert fs_with_future.mid_price == pytest.approx(0.41)
    assert fs_with_future.mid_price == fs_without_future.mid_price
    assert fs_with_future.n_snapshots == 1, (
        "el snapshot futuro nunca debe contar"
    )


def test_features_use_only_past_when_present_in_history():
    """delta_p_5m: usar el snapshot de hace ~5 min, no uno posterior a as_of."""
    snaps = [
        _snap(seconds_ago=600, bid=0.30, ask=0.32),  # t-10m
        _snap(seconds_ago=300, bid=0.40, ask=0.42),  # t-5m (target de delta_p_5m)
        _snap(seconds_ago=60, bid=0.50, ask=0.52),  # t-1m
        _snap(seconds_ago=0, bid=0.60, ask=0.62),  # current
    ]
    future = SnapshotInput(
        ts=BASE_TS + timedelta(minutes=1),
        best_bid=0.99,
        best_ask=0.99,
        last_trade_price=0.99,
        spread=0.0,
        volume_24hr=10.0,
    )

    fs = calculate_features([*snaps, future], as_of=BASE_TS)

    assert fs.mid_price == pytest.approx(0.61)
    assert fs.delta_p_1m == pytest.approx(0.61 - 0.51)  # 0.10
    assert fs.delta_p_5m == pytest.approx(0.61 - 0.41)  # 0.20


def test_returns_none_when_no_history():
    fs = calculate_features([], as_of=BASE_TS)
    assert fs.mid_price is None
    assert fs.delta_p_1m is None
    assert fs.delta_p_5m is None
    assert fs.n_snapshots == 0


def test_returns_none_when_lookback_window_empty():
    """Solo hay un snapshot al momento as_of: no hay 1m de historia → delta_p_1m=None."""
    only = _snap(seconds_ago=0, bid=0.50, ask=0.52)
    fs = calculate_features([only], as_of=BASE_TS)
    assert fs.mid_price == pytest.approx(0.51)
    assert fs.delta_p_1m is None
    assert fs.delta_p_5m is None
    assert fs.mid_velocity is None  # necesita 2 snapshots


def test_mid_velocity_uses_previous_snapshot_only():
    s1 = _snap(seconds_ago=30, bid=0.50, ask=0.52)
    s2 = _snap(seconds_ago=0, bid=0.52, ask=0.54)
    fs = calculate_features([s1, s2], as_of=BASE_TS)
    assert fs.mid_velocity == pytest.approx((0.53 - 0.51) / 30.0)


def test_spread_expansion_returns_none_with_thin_history():
    """Con menos de 5 puntos no se computa z-score (volatilidad inestable)."""
    snaps = [
        _snap(seconds_ago=240, bid=0.40, ask=0.42),
        _snap(seconds_ago=180, bid=0.40, ask=0.42),
        _snap(seconds_ago=0, bid=0.40, ask=0.42),
    ]
    fs = calculate_features(snaps, as_of=BASE_TS)
    assert fs.spread_expansion is None
