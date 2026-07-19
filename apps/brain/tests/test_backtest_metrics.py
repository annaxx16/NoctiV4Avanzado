"""Tests de las métricas de validación (puras)."""

from __future__ import annotations

import math

import pytest

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
    # Con capital 100: equity 110, 106 (pico 110 → dd 0.036), 116, 112 (dd 0.034)
    assert max_drawdown([10, -4, 10, -4], 100.0) == pytest.approx(4 / 110)
    assert max_drawdown([5, 5, 5], 100.0) == 0.0  # monótona creciente


def test_max_drawdown_racha_perdedora_inicial():
    """Regresión: la equity partía de 0, el pico se quedaba en 0 y cualquier
    pérdida desde el primer trade reportaba 0.0 de drawdown."""
    # equity: 90, 80, 70, 75 sobre capital 100 → peor giveback 30/100
    assert max_drawdown([-10, -10, -10, 5], 100.0) == pytest.approx(0.30)


def test_max_drawdown_hoyo_inicial_con_recuperacion():
    """Regresión (el caso que rompía `edge_weights`): un edge net-positivo que
    casi revienta antes de remontar reportaba max_dd = 0.0 → stability máxima."""
    # equity: 70, 50, 110, 130 sobre capital 100 → peor giveback 50/100
    assert max_drawdown([-30, -20, 60, 20], 100.0) == pytest.approx(0.50)


def test_max_drawdown_exige_capital_positivo():
    """Un drawdown fraccional no está definido sin base de capital."""
    with pytest.raises(ValueError):
        max_drawdown([1.0, -1.0], 0.0)


def test_sharpe():
    assert sharpe([0.1]) == 0.0  # <2 trades
    assert sharpe([0.1, 0.1, 0.1]) == 0.0  # std 0
    assert sharpe([0.2, -0.1, 0.3, 0.0]) > 0


def test_compute_metrics_y_aceptacion():
    pnls = [5.0, 5.0, 5.0, -2.0]
    returns = [0.5, 0.5, 0.5, -0.2]
    preds = [0.7, 0.7, 0.7, 0.3]
    outs = [1, 1, 1, 1]
    r = compute_metrics(pnls, returns, preds, outs, initial_capital=100.0)
    assert r.n_trades == 4
    assert r.n_wins == 3
    assert r.hit_rate == 0.75
    assert r.total_pnl_usd == 13.0
    assert r.profit_factor == 7.5  # 15 / 2
    assert r.brier is not None


def test_aceptacion_rechaza_estrategia_perdedora():
    """Regresión del gate: una estrategia que solo pierde reportaba max_dd=0.0
    y pasaba el criterio de drawdown."""
    pnls = [-10.0, -10.0, -10.0, 5.0]
    r = compute_metrics(
        pnls, [-1.0, -1.0, -1.0, 0.5], [0.6] * 4, [0, 0, 0, 1], initial_capital=100.0
    )
    assert r.max_drawdown == pytest.approx(0.30)
    assert not r.passes_acceptance()


def test_aceptacion_comprueba_sharpe():
    """El Sharpe figuraba en los criterios documentados pero no se evaluaba."""
    # PF y drawdown holgados, pero retornos ruidosos → Sharpe por debajo del mínimo.
    pnls = [30.0, -20.0, 30.0, -20.0, 30.0, -18.0]
    returns = [3.0, -2.0, 3.0, -2.0, 3.0, -1.8]
    # Predicciones calibradas a propósito: con outcomes alternos ninguna
    # predicción constante baja de 0.25 de Brier, y el gate cortaría ahí sin
    # llegar nunca a evaluar el Sharpe, que es lo que este test comprueba.
    preds = [0.8, 0.2, 0.8, 0.2, 0.8, 0.2]
    r = compute_metrics(
        pnls, returns, preds, [1, 0, 1, 0, 1, 0], initial_capital=1000.0
    )
    assert r.profit_factor >= 1.5
    assert r.sharpe < 1.0  # el umbral heredado de renta variable, inalcanzable aquí
    assert r.passes_acceptance(sharpe_min=0.0)  # sin gate de Sharpe, pasaría
    assert not r.passes_acceptance(sharpe_min=1.0)  # con el gate literal, nunca
