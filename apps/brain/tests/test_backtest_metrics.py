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
    # Referencia de mercado que el modelo bate holgadamente: sin ella el gate
    # cortaría en la compuerta de calibración y no llegaría nunca al Sharpe, que
    # es lo que este test comprueba.
    base = [0.5] * 6
    r = compute_metrics(
        pnls, returns, preds, [1, 0, 1, 0, 1, 0],
        initial_capital=1000.0, baselines=base,
    )
    assert r.profit_factor >= 1.5
    assert r.sharpe < 1.0  # el umbral heredado de renta variable, inalcanzable aquí
    assert r.passes_acceptance(sharpe_min=0.0, min_predictions=1)
    assert not r.passes_acceptance(sharpe_min=1.0, min_predictions=1)


# ---------------------------------------------------------------------------
# El gate de calibración es RELATIVO
#
# «Brier < 0.20» lo cumple un sistema sin edge. Medido el 28/07/2026 sobre 1.799
# eventos reales: modelo 0.0723, precio de mercado 0.0722. Los dos aprobaban un
# gate de 0.20 y la diferencia entre ellos era cero.
# ---------------------------------------------------------------------------


def _retornos(n):
    """Retornos con varianza. Constantes darían Sharpe 0 y el gate cortaría ahí,
    tapando justo la compuerta que estos tests quieren ejercitar."""
    return [1.0 if i % 2 else 0.8 for i in range(n)]


def _perfecto(n=400):
    """Modelo que acierta; mercado que se queda en 0.5."""
    outcomes = [i % 2 for i in range(n)]
    preds = [0.95 if o else 0.05 for o in outcomes]
    base = [0.5] * n
    return preds, base, outcomes


def test_un_modelo_que_copia_al_mercado_no_bate_al_mercado():
    """El caso que el gate viejo dejaba pasar.

    `compute_p_fair` es hoy un passthrough de la EMA del mid, o sea una versión
    suavizada del precio. Predice casi igual que el mercado — y eso no es edge.
    """
    from umbra.backtest.metrics import brier_skill

    outcomes = [i % 2 for i in range(400)]
    mercado = [0.9 if o else 0.1 for o in outcomes]
    modelo = [p + 0.001 for p in mercado]  # una pizca distinto, sin información

    sk = brier_skill(modelo, mercado, outcomes)
    assert sk is not None
    assert sk.brier_model < 0.20, "supera el umbral absoluto viejo..."
    assert sk.brier_baseline < 0.20, "...y el mercado también"
    assert sk.beats_baseline is False, "pero no bate al mercado, que es lo que importa"


def test_un_modelo_con_informacion_real_si_bate_al_mercado():
    from umbra.backtest.metrics import brier_skill

    sk = brier_skill(*_perfecto())
    assert sk is not None
    assert sk.diff_mean < 0
    assert sk.ci_hi < 0
    assert sk.beats_baseline is True


def test_el_intervalo_que_cruza_cero_no_es_evidencia():
    """Los números reales del 28/07/2026: diff -0.00222, IC [-0.00564, +0.00056]."""
    from umbra.backtest.metrics import BrierSkill

    sk = BrierSkill(
        n=136, brier_model=0.1104, brier_baseline=0.1126,
        diff_mean=-0.00222, ci_lo=-0.00564, ci_hi=0.00056,
    )
    assert sk.diff_mean < 0, "el punto estimado favorece al modelo"
    assert sk.beats_baseline is False, "pero el intervalo cruza cero: es ruido"


def test_el_bootstrap_es_determinista():
    """Un gate go/no-go no puede dar dos veredictos sobre los mismos datos."""
    from umbra.backtest.metrics import brier_skill

    args = _perfecto()
    assert brier_skill(*args).ci_hi == brier_skill(*args).ci_hi


def test_sin_referencia_de_mercado_el_gate_no_pasa():
    """Fail-closed: no poder afirmar no es poder afirmar que sí."""
    from umbra.backtest.metrics import compute_metrics

    preds, _, outcomes = _perfecto()
    n = len(preds)
    rep = compute_metrics(
        [1.0] * n, _retornos(n), preds, outcomes, initial_capital=1000.0
    )
    assert rep.skill is None
    assert rep.passes_acceptance() is False


def test_con_referencia_y_edge_real_el_gate_pasa():
    from umbra.backtest.metrics import compute_metrics

    preds, base, outcomes = _perfecto()
    n = len(preds)
    rep = compute_metrics(
        [1.0] * n, _retornos(n), preds, outcomes,
        initial_capital=1000.0, baselines=base,
    )
    assert rep.skill is not None and rep.skill.beats_baseline
    assert rep.passes_acceptance() is True


def test_una_muestra_pequeña_no_pasa_aunque_el_intervalo_excluya_cero():
    """No confundir «no hay edge» con «no hay datos»."""
    from umbra.backtest.metrics import compute_metrics

    preds, base, outcomes = _perfecto(n=40)
    rep = compute_metrics(
        [1.0] * 40, _retornos(40), preds, outcomes,
        initial_capital=1000.0, baselines=base,
    )
    assert rep.passes_acceptance(min_predictions=200) is False
    assert rep.passes_acceptance(min_predictions=10) is True
