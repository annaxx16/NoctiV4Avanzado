"""El pico de equity contra el que se mide el drawdown.

Es el insumo de las compuertas 2 y 3 del risk engine —halt y throttle—, así que un
pico mal calculado no da un número feo en un dashboard: deja el bot operando en una
racha perdedora que debería haberlo parado.

La noche del 28/07/2026 pasó exactamente eso. El pico se medía «desde la última vez
que la cartera estuvo plana», y esta estrategia se queda plana constantemente: 102
veces en 24 horas. Cada una reseteaba el pico a la equity del momento y el drawdown
a cero. La equity cayó un 15,82% y la compuerta nunca vio más de un 14,62%, con el
halt puesto en el 15%. Se libró por 38 centésimas.

Estos tests no tocan Postgres: parchean los ayudantes de consulta en el módulo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from umbra.portfolio import manager


async def _async(value):
    return value


def _view(cost: float = 100.0, value: float | None = None):
    """Una posición abierta. Solo importan los campos que `portfolio_snapshot` suma."""
    from types import SimpleNamespace

    return SimpleNamespace(
        total_cost_usd=cost,
        current_value_usd=cost if value is None else value,
        unrealized_pnl_usd=0.0 if value is None else value - cost,
    )


@pytest.fixture
def world(monkeypatch):
    """Cartera con equity controlable y un pico histórico que el test decide.

    `peak_calls` recoge el `since_ts` con el que se consultó el pico: la diferencia
    entre la versión rota y la arreglada está justo ahí.
    """
    from types import SimpleNamespace

    state = SimpleNamespace(views=[], realized=0.0, peak_hist=0.0, peak_calls=[])

    async def _peak(_session, since_ts=None):
        state.peak_calls.append(since_ts)
        return state.peak_hist

    monkeypatch.setattr(manager, "position_views", lambda _s, include_closed=False: _async(state.views))
    monkeypatch.setattr(manager, "_realized_total", lambda _s: _async(state.realized))
    monkeypatch.setattr(manager, "_peak_equity", _peak)
    monkeypatch.setattr(manager.settings, "bankroll_usd", 1000.0)
    monkeypatch.setattr(manager.settings, "dd_peak_window_hours", 48.0)
    return state


# ---------------------------------------------------------------------------
# La regresión del 28/07/2026
# ---------------------------------------------------------------------------


async def test_estar_plano_no_borra_el_drawdown(world):
    """EL TEST QUE FALTABA.

    Cartera plana —sin posiciones abiertas— después de perder. La versión anterior
    hacía `peak = equity` en esta rama exacta y devolvía 0.0. Si este test se pone
    verde con un drawdown de 0, el freno vuelve a estar ciego.
    """
    world.views = []           # plana, como en 102 momentos de aquella noche
    world.realized = -158.0    # se perdió llegando hasta aquí
    world.peak_hist = 1000.0   # el pico sigue en la ventana

    snap = await manager.portfolio_snapshot(None)

    assert snap.equity_usd == pytest.approx(842.0)
    assert snap.peak_equity_usd == pytest.approx(1000.0)
    assert snap.drawdown_pct == pytest.approx(-0.158)
    assert snap.drawdown_pct != 0.0, "estar plano un instante no borra lo perdido"


async def test_la_caida_de_aquella_noche_habria_disparado_el_halt(world, monkeypatch):
    """Los números reales: 4.853,46 -> 4.085,63 es un -15,82%, y el halt está al 15%."""
    bankroll = 4853.46
    monkeypatch.setattr(manager.settings, "bankroll_usd", bankroll)
    world.views = []
    world.realized = 4085.63 - bankroll
    world.peak_hist = 4853.46

    snap = await manager.portfolio_snapshot(None)

    assert snap.drawdown_pct == pytest.approx(-0.1582, abs=1e-4)
    assert snap.drawdown_pct <= -0.15, "por debajo del umbral: la compuerta 2 debe morder"


# ---------------------------------------------------------------------------
# La ventana
# ---------------------------------------------------------------------------


async def test_el_pico_se_busca_en_una_ventana_de_reloj(world):
    """No `None` (todo el histórico) ni el instante del último plano: 48h atrás."""
    world.views = [_view()]
    world.peak_hist = 1000.0
    before = datetime.now(UTC)

    await manager.portfolio_snapshot(None)

    assert len(world.peak_calls) == 1
    since = world.peak_calls[0]
    assert since is not None, "sin ventana se miraría el histórico entero"
    esperado = before - timedelta(hours=48)
    assert abs((since - esperado).total_seconds()) < 5


async def test_la_ventana_es_configurable(world, monkeypatch):
    monkeypatch.setattr(manager.settings, "dd_peak_window_hours", 24.0)
    world.views = [_view()]
    before = datetime.now(UTC)

    await manager.portfolio_snapshot(None)

    since = world.peak_calls[0]
    assert abs((since - (before - timedelta(hours=24))).total_seconds()) < 5


async def test_un_pico_fuera_de_la_ventana_no_deja_el_bot_en_halt_eterno(world):
    """La intención original, conservada.

    Un pico viejísimo no debe condenar al bot para siempre. Ahora quien lo hace
    caducar es el reloj y no el churn de la estrategia: `_peak_equity` simplemente
    no lo devuelve porque cae fuera de la ventana.
    """
    world.views = [_view()]
    world.realized = 0.0
    world.peak_hist = 0.0  # nada dentro de la ventana

    snap = await manager.portfolio_snapshot(None)

    assert snap.peak_equity_usd == pytest.approx(snap.equity_usd)
    assert snap.drawdown_pct == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Comportamiento normal
# ---------------------------------------------------------------------------


async def test_una_equity_en_maximos_no_tiene_drawdown(world):
    world.views = [_view()]
    world.realized = 200.0     # equity 1200
    world.peak_hist = 1000.0   # el máximo anterior era menor

    snap = await manager.portfolio_snapshot(None)

    assert snap.peak_equity_usd == pytest.approx(1200.0)
    assert snap.drawdown_pct == pytest.approx(0.0)


async def test_el_drawdown_se_mide_tambien_con_posiciones_abiertas(world):
    world.views = [_view(cost=300.0, value=200.0)]  # -100 no realizado
    world.realized = 0.0
    world.peak_hist = 1000.0

    snap = await manager.portfolio_snapshot(None)

    # cash = 1000 + 0 - 300 = 700 ; equity = 700 + 200 = 900
    assert snap.equity_usd == pytest.approx(900.0)
    assert snap.drawdown_pct == pytest.approx(-0.10)
