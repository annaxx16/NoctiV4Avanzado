"""Tests de las métricas de validación (puras)."""

from __future__ import annotations

import math

from umbra.backtest.metrics import (
    brier_score,
    compute_metrics,
    max_drawdown,
    profit_factor,
    sharpe,
)


def test_brier_perfecto_y_baseline():
    assert brier_score([1.0, 0.0], [1, 0]) == 0.0
    assert brier_score([0.5, 0.5], [1, 0]) == 0.25
    assert brier_score([], []) is None


def test_profit_factor():
    assert profit_factor([10, -5, 5]) == 3.0  # 15 / 5
    assert profit_factor([5, 10]) == math.inf  # sin pérdidas
    assert profit_factor([-1, -2]) == 0.0  # sin ganancias


def test_max_drawdown():
    # equity: 10, 6 (pico 10 → dd 0.4), 16, 12 (pico 16 → dd 0.25)
    assert max_drawdown([10, -4, 10, -4]) == 0.4
    assert max_drawdown([5, 5, 5]) == 0.0  # monótona creciente


def test_sharpe():
    assert sharpe([0.1]) == 0.0  # <2 trades
    assert sharpe([0.1, 0.1, 0.1]) == 0.0  # std 0
    assert sharpe([0.2, -0.1, 0.3, 0.0]) > 0


def test_compute_metrics_y_aceptacion():
    pnls = [5.0, 5.0, 5.0, -2.0]
    returns = [0.5, 0.5, 0.5, -0.2]
    preds = [0.7, 0.7, 0.7, 0.3]
    outs = [1, 1, 1, 1]
    r = compute_metrics(pnls, returns, preds, outs)
    assert r.n_trades == 4
    assert r.n_wins == 3
    assert r.hit_rate == 0.75
    assert r.total_pnl_usd == 13.0
    assert r.profit_factor == 7.5  # 15 / 2
    assert r.brier is not None
