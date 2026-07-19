"""Tests del motor de backtesting (puro, sin DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial

from umbra.backtest.engine import kelly_sizer, run_backtest
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


# ---------------------------------------------------------------------------
# Sizing de producción y orden cronológico (A2)
# ---------------------------------------------------------------------------


def test_sizer_fn_reemplaza_el_notional_plano():
    """Con `sizer_fn` el tamaño lo decide el sizer, no el notional fijo."""
    markets = {"0xtest_spike": _market_with_spike_up()}
    outcomes = {"0xtest_spike": False}

    res = run_backtest(
        markets,
        outcomes,
        partial(detect_overreaction, sigma_threshold=3.0),
        notional_usd=10.0,
        sizer_fn=lambda side, p_fair, price: 42.0,
    )
    assert res.trades, "el edge debería disparar"
    assert all(t.notional_usd == 42.0 for t in res.trades)
    # `ret` se normaliza contra el notional real, no contra el plano.
    for t in res.trades:
        assert t.ret == t.pnl_usd / 42.0


def test_sizer_que_veta_no_genera_trade():
    """Notional 0 = Kelly no ve apuesta; espeja el gate 7 del risk engine."""
    markets = {"0xtest_spike": _market_with_spike_up()}
    outcomes = {"0xtest_spike": False}

    res = run_backtest(
        markets,
        outcomes,
        partial(detect_overreaction, sigma_threshold=3.0),
        sizer_fn=lambda side, p_fair, price: 0.0,
    )
    assert res.trades == []


def test_kelly_sizer_respeta_el_cap_por_trade():
    markets = {"0xtest_spike": _market_with_spike_up()}
    outcomes = {"0xtest_spike": False}

    res = run_backtest(
        markets,
        outcomes,
        partial(detect_overreaction, sigma_threshold=3.0),
        sizer_fn=kelly_sizer(bankroll=1_000_000.0, kappa=1.0, max_risk_per_trade_usd=25.0),
    )
    assert res.trades, "el edge debería disparar"
    assert all(t.notional_usd <= 25.0 for t in res.trades)


def test_trades_salen_en_orden_cronologico():
    """El bucle es market-major; sin ordenar, la curva de equity que consumen
    las métricas no es una serie temporal y el drawdown es ficticio."""
    markets = {
        "0xtest_b_tarde": [_snap(i + 100, p.last_trade_price) for i, p in
                           enumerate(_market_with_spike_up())],
        "0xtest_a_pronto": _market_with_spike_up(),
    }
    outcomes = {"0xtest_b_tarde": False, "0xtest_a_pronto": False}

    res = run_backtest(
        markets, outcomes, partial(detect_overreaction, sigma_threshold=3.0)
    )
    assert len(res.trades) >= 2, "hacen falta trades de ambos mercados"
    ts = [t.entry_ts for t in res.trades]
    assert ts == sorted(ts)


# ---------------------------------------------------------------------------
# Ventana de datos por mercado (M1)
# ---------------------------------------------------------------------------


def test_no_reemite_señal_sobre_datos_congelados():
    """Pasado el último snapshot, `visible` deja de cambiar y el detector volvía
    a emitir la misma señal una vez por cooldown hasta el `end` global. Un spike
    aislado daba 1 trade o 169 según lo lejos que llegara la ventana."""
    markets = {"0xtest_spike": _market_with_spike_up()}
    outcomes = {"0xtest_spike": False}
    detect_fn = partial(detect_overreaction, sigma_threshold=3.0)
    ultimo_snapshot = BASE + timedelta(minutes=15)

    conteos = {
        dias: len(
            run_backtest(
                markets,
                outcomes,
                detect_fn,
                end=ultimo_snapshot + timedelta(days=dias),
                step_minutes=5,
            ).trades
        )
        for dias in (0, 1, 7)
    }
    assert conteos == {0: 1, 1: 1, 7: 1}, conteos


def test_mercado_corto_no_contamina_a_uno_largo():
    """El `end` global lo fija el mercado más largo. Un mercado que resolvía
    pronto veía su única señal replicada durante todo el resto del histórico."""
    corto = _market_with_spike_up()
    largo = [_snap(i, 0.30 + _NOISE[i % len(_NOISE)]) for i in range(3000)]
    markets = {"0xtest_corto": corto, "0xtest_largo": largo}
    outcomes = {"0xtest_corto": False, "0xtest_largo": True}

    res = run_backtest(
        markets, outcomes, partial(detect_overreaction, sigma_threshold=3.0)
    )
    del_corto = [t for t in res.trades if t.market_id == "0xtest_corto"]
    assert len(del_corto) == 1


def test_ventana_por_mercado_no_altera_los_trades_dentro_de_ella():
    """El recorte solo elimina eval_ts que no podían producir nada distinto: el
    grid sigue anclado al `start` global y el trade es el mismo."""
    markets = {"0xtest_spike": _market_with_spike_up()}
    outcomes = {"0xtest_spike": False}
    res = run_backtest(
        markets,
        outcomes,
        partial(detect_overreaction, sigma_threshold=3.0),
        step_minutes=5,
    )
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_ts == BASE + timedelta(minutes=15)
    assert t.side == "BUY_NO"


# ---------------------------------------------------------------------------
# Calibración sin sesgo de selección (A3)
# ---------------------------------------------------------------------------


def test_brier_puntua_señales_vetadas_por_el_sizer():
    """El Brier mide al modelo, no al gestor de riesgo. Si solo puntuara los
    trades, el sizer —que veta las señales de menor convicción— dejaría fuera
    justo las predicciones más flojas y la calibración saldría mejor de lo real.
    """
    markets = {"0xtest_spike": _market_with_spike_up()}
    outcomes = {"0xtest_spike": False}

    res = run_backtest(
        markets,
        outcomes,
        partial(detect_overreaction, sigma_threshold=3.0),
        sizer_fn=lambda side, p_fair, price: 0.0,  # veta todo
    )
    assert res.trades == []
    # Sin trades, pero la predicción existió y se puntúa.
    assert res.metrics.n_predictions == 1
    assert res.metrics.brier is not None


def test_n_predictions_coincide_con_trades_si_nada_se_veta():
    markets = {"0xtest_spike": _market_with_spike_up()}
    outcomes = {"0xtest_spike": False}
    res = run_backtest(
        markets, outcomes, partial(detect_overreaction, sigma_threshold=3.0)
    )
    assert res.metrics.n_predictions == res.metrics.n_trades == 1
