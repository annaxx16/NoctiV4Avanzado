"""Tests del edge OverreactionV1."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from umbra.edges.overreaction import detect
from umbra.features.calculator import SnapshotInput

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _stable_then_spike(price_stable: float, price_spike: float, n_stable: int = 12):
    """n_stable snapshots con ruido pequeño alrededor de price_stable + 1 spike."""
    snaps = []
    noise = [0.0, 0.001, -0.001, 0.002, -0.002, 0.001, -0.001, 0.0, 0.002, -0.001, 0.001, 0.0]
    for i in range(n_stable):
        p = price_stable + noise[i % len(noise)]
        snaps.append(
            SnapshotInput(
                ts=BASE - timedelta(seconds=(n_stable - i) * 30),
                best_bid=p - 0.005,
                best_ask=p + 0.005,
                last_trade_price=p,
                spread=0.01,
                volume_24hr=1000.0,
            )
        )
    snaps.append(
        SnapshotInput(
            ts=BASE,
            best_bid=price_spike - 0.005,
            best_ask=price_spike + 0.005,
            last_trade_price=price_spike,
            spread=0.01,
            volume_24hr=1000.0,
        )
    )
    return snaps


def test_returns_none_when_history_too_short():
    snaps = _stable_then_spike(0.5, 0.55, n_stable=3)
    assert detect(snaps, BASE) is None


def test_returns_none_when_no_overreaction():
    # precios muy estables + ruido mínimo → no debería disparar
    snaps = []
    for i in range(12):
        p = 0.500 + (i % 3 - 1) * 0.001  # ±0.001 oscilación
        snaps.append(
            SnapshotInput(
                ts=BASE - timedelta(seconds=(12 - i) * 30),
                best_bid=p - 0.005,
                best_ask=p + 0.005,
                last_trade_price=p,
                spread=0.01,
                volume_24hr=1000.0,
            )
        )
    snaps.append(
        SnapshotInput(
            ts=BASE,
            best_bid=0.500 - 0.005,
            best_ask=0.500 + 0.005,
            last_trade_price=0.500,
            spread=0.01,
            volume_24hr=1000.0,
        )
    )
    assert detect(snaps, BASE) is None


def test_detects_spike_up_as_buy_no():
    snaps = _stable_then_spike(0.30, 0.45)  # spike de 30% a 45%
    out = detect(snaps, BASE)
    assert out is not None
    assert out.side == "BUY_NO"
    assert out.market_price == pytest.approx(0.45)
    assert out.fair_price < out.market_price
    assert out.edge_value > 0
    assert out.strength > 3.0


def test_detects_spike_down_as_buy_yes():
    snaps = _stable_then_spike(0.70, 0.55)
    out = detect(snaps, BASE)
    assert out is not None
    assert out.side == "BUY_YES"
    assert out.market_price == pytest.approx(0.55)
    assert out.fair_price > out.market_price
    assert out.strength < -3.0


def test_ignores_future_snapshots():
    snaps = _stable_then_spike(0.30, 0.45)
    future = SnapshotInput(
        ts=BASE + timedelta(minutes=10),
        best_bid=0.99,
        best_ask=0.99,
        last_trade_price=0.99,
        spread=0.0,
        volume_24hr=10.0,
    )
    out_with = detect([*snaps, future], BASE)
    out_without = detect(snaps, BASE)
    assert out_with is not None
    assert out_without is not None
    assert out_with.market_price == out_without.market_price
    assert out_with.fair_price == pytest.approx(out_without.fair_price)
