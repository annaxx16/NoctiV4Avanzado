"""Las 11 compuertas de `risk/engine.py`, una por una.

Es lo más crítico del sistema y era lo menos cubierto: ni un solo test unitario.
`check()` decide si una señal se convierte en dinero y cuánto, y hasta ahora solo
se ejercitaba de refilón desde los tests de orquestación, que no aíslan nada.

Estos tests no tocan Postgres ni Redis. `check()` llama a sus ayudantes de consulta
por nombre de módulo, así que se parchean en `umbra.risk.engine` y la sesión puede
ser `None`. Cada test parte de un mundo permisivo —donde la señal pasaría— y aprieta
exactamente una tuerca. Si un test falla, la compuerta que falla es la que da nombre
al test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from umbra.risk import engine
from umbra.risk.sizer import SizingResult

MARKET = "0xtest_risk_engine"

# Kelly ya aplicado: $20 de nocional, 100 shares. Los gates 8-11 lo recortan.
SIZING = SizingResult(f_star=0.1, shares=100.0, notional_usd=20.0)
EDGE = 0.05  # > min_edge (0.02)


def _book(**over):
    """Un book fresco, líquido y estrecho. Pasa la compuerta 6 sin discusión."""
    base = {
        "ts": datetime.now(UTC),
        "spread": 0.01,
        "liquidity_num": 10_000.0,
        "volume_24hr": 50_000.0,
    }
    base.update(over)
    return SimpleNamespace(**base)


async def _async(value):
    return value


# Los umbrales contra los que están escritos estos tests. NO son los defaults de
# `config.py`: son un mundo cerrado y explícito.
#
# `settings` se hidrata desde el `.env` de la raíz, que es el del operador y hoy
# ya pisa seis de estos valores (`MIN_EDGE=0.003`, `MAX_RISK_PER_TRADE_USD=60`…).
# Sin este fixture, el suite que valida el risk engine dependería de la config de
# riesgo en vivo, y bajar un límite en producción rompería tests que no hablan de
# ese límite. Los tests que aprietan una tuerca la reescriben encima de esto.
_PINNED = {
    "mode": "sim",
    "bankroll_usd": 1000.0,
    "min_edge": 0.02,
    "min_signal_confidence": 0.30,
    "max_risk_per_trade_usd": 50.0,
    "max_exposure_per_market_usd": 200.0,
    "max_gross_exposure_pct": 0.50,
    "min_cash_reserve_pct": 0.10,
    "dd_throttle_pct": 0.10,
    "dd_halt_pct": 0.15,
    "cooldown_minutes": 30.0,
    "stale_book_max_age_sec": 180,
    "max_spread_for_entry": 0.04,
    "min_liquidity_for_entry_usd": 3000.0,
    "max_time_to_resolution_hours_floor": 2.0,
}


@pytest.fixture(autouse=True)
def _pinned_settings(monkeypatch):
    for key, value in _PINNED.items():
        monkeypatch.setattr(engine.settings, key, value)


@pytest.fixture
def world(monkeypatch):
    """El mundo donde todo está bien. Cada test estropea una sola cosa.

    Los ayudantes parcheados leen `state` en el momento de la llamada, así que
    basta con reasignar un atributo del `state` que devuelve el fixture.
    """
    state = SimpleNamespace(
        halted=False,
        set_halt_calls=[],
        drawdown=0.0,
        open_position=None,
        last_close=None,
        book=_book(),
        end_date=datetime.now(UTC) + timedelta(days=30),
        market_cost=0.0,
        gross=0.0,
        realized=0.0,
    )

    async def _set_halt(active):
        state.set_halt_calls.append(active)

    monkeypatch.setattr(engine, "is_halted", lambda: _async(state.halted))
    monkeypatch.setattr(engine, "set_halt", _set_halt)
    monkeypatch.setattr(engine, "current_drawdown_pct", lambda _s: _async(state.drawdown))
    monkeypatch.setattr(engine, "open_position_for", lambda _s, _m, _side: _async(state.open_position))
    monkeypatch.setattr(engine, "last_close_ts_for_market", lambda _s, _m: _async(state.last_close))
    monkeypatch.setattr(engine, "fresh_book", lambda _s, _m: _async(state.book))
    monkeypatch.setattr(engine, "market_end_date", lambda _s, _m: _async(state.end_date))
    monkeypatch.setattr(engine, "market_open_cost", lambda _s, _m: _async(state.market_cost))
    monkeypatch.setattr(engine, "gross_exposure", lambda _s: _async(state.gross))
    monkeypatch.setattr(engine, "realized_pnl_total", lambda _s: _async(state.realized))
    return state


async def _check(**over):
    kwargs = {
        "session": None,
        "condition_id": MARKET,
        "edge_value": EDGE,
        "sizing": SIZING,
        "side": "BUY_YES",
        "confidence": 0.9,
    }
    kwargs.update(over)
    return await engine.check(**kwargs)


# ---------------------------------------------------------------------------
# El caso base: sin él, cualquier test de rechazo pasaría por el motivo equivocado.
# ---------------------------------------------------------------------------


async def test_happy_path_accepts_untouched_sizing(world):
    d = await _check()
    assert d.accepted is True
    assert d.reason == "ok"
    assert d.adjusted_notional_usd == pytest.approx(20.0)
    assert d.adjusted_shares == pytest.approx(100.0)
    assert d.kappa_factor == 1.0


# ---------------------------------------------------------------------------
# 1. Kill switch
# ---------------------------------------------------------------------------


async def test_gate_01_kill_switch_blocks(world):
    world.halted = True
    d = await _check()
    assert d.accepted is False
    assert d.reason == "kill_switch_active"
    assert d.adjusted_notional_usd == 0.0


async def test_gate_01_kill_switch_precedes_every_other_gate(world):
    """Con el kill switch puesto, ni se consulta el drawdown ni el book."""
    world.halted = True
    world.drawdown = -0.99
    world.book = None  # provocaría `no_book_snapshot` si se llegase a mirar
    d = await _check(edge_value=-1.0)
    assert d.reason == "kill_switch_active"


async def test_gate_01_is_halted_fails_closed_in_live(monkeypatch):
    """Si Redis no responde en `live`, se asume haltado. Fail-CLOSED."""

    class _DeadRedis:
        async def get(self, _key):
            raise ConnectionError("Redis no responde")

    monkeypatch.setattr(engine, "get_redis", lambda: _DeadRedis())
    monkeypatch.setattr(engine.settings, "mode", "live")
    assert await engine.is_halted() is True


async def test_gate_01_is_halted_fails_open_in_sim_by_default(monkeypatch):
    """En `sim`, un Redis caído no debe parar la investigación. Es la excepción."""

    class _DeadRedis:
        async def get(self, _key):
            raise ConnectionError("Redis no responde")

    monkeypatch.setattr(engine, "get_redis", lambda: _DeadRedis())
    monkeypatch.setattr(engine.settings, "mode", "sim")
    monkeypatch.setattr(engine.settings, "redis_fail_closed_in_sim", False)
    assert await engine.is_halted() is False

    monkeypatch.setattr(engine.settings, "redis_fail_closed_in_sim", True)
    assert await engine.is_halted() is True


# ---------------------------------------------------------------------------
# 2. Drawdown halt
# ---------------------------------------------------------------------------


async def test_gate_02_drawdown_halt_blocks_and_arms_the_kill_switch(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "dd_halt_pct", 0.15)
    world.drawdown = -0.20
    d = await _check()
    assert d.accepted is False
    assert d.reason.startswith("auto_halt_dd")
    # No basta con rechazar la señal: el halt queda puesto para todo el sistema.
    assert world.set_halt_calls == [True]


async def test_gate_02_drawdown_exactly_at_the_halt_threshold_blocks(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "dd_halt_pct", 0.15)
    world.drawdown = -0.15
    d = await _check()
    assert d.reason.startswith("auto_halt_dd")


# ---------------------------------------------------------------------------
# 3. Drawdown throttle
# ---------------------------------------------------------------------------


async def test_gate_03_drawdown_throttle_halves_the_size(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "dd_throttle_pct", 0.10)
    monkeypatch.setattr(engine.settings, "dd_halt_pct", 0.15)
    world.drawdown = -0.12  # entre throttle y halt
    d = await _check()
    assert d.accepted is True
    assert d.kappa_factor == 0.5
    assert d.adjusted_notional_usd == pytest.approx(10.0)
    assert d.adjusted_shares == pytest.approx(50.0)


async def test_gate_03_no_throttle_above_the_threshold(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "dd_throttle_pct", 0.10)
    world.drawdown = -0.09
    d = await _check()
    assert d.kappa_factor == 1.0
    assert d.adjusted_notional_usd == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# 4. No averaging down
# ---------------------------------------------------------------------------


async def test_gate_04_rejects_when_position_already_open_same_side(world):
    world.open_position = SimpleNamespace(shares=42.0)
    d = await _check(side="BUY_YES")
    assert d.accepted is False
    assert d.reason.startswith("position_already_open")
    assert "42" in d.reason


async def test_gate_04_skipped_when_side_is_none(world):
    """`side=None` viene del orchestrator viejo. La compuerta no puede consultarse."""
    world.open_position = SimpleNamespace(shares=42.0)
    d = await _check(side=None)
    assert d.accepted is True


# ---------------------------------------------------------------------------
# 5. Cooldown post-exit
# ---------------------------------------------------------------------------


async def test_gate_05_cooldown_blocks_reentry(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "cooldown_minutes", 30.0)
    world.last_close = datetime.now(UTC) - timedelta(minutes=5)
    d = await _check()
    assert d.accepted is False
    assert d.reason.startswith("cooldown until")


async def test_gate_05_cooldown_expired_allows_reentry(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "cooldown_minutes", 30.0)
    world.last_close = datetime.now(UTC) - timedelta(minutes=31)
    d = await _check()
    assert d.accepted is True


# ---------------------------------------------------------------------------
# 6. Book: existencia, frescura, spread, liquidez
# ---------------------------------------------------------------------------


async def test_gate_06_no_book_snapshot_rejects(world):
    world.book = None
    d = await _check()
    assert d.reason == "no_book_snapshot"


async def test_gate_06_stale_book_rejects(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "stale_book_max_age_sec", 180)
    world.book = _book(ts=datetime.now(UTC) - timedelta(seconds=200))
    d = await _check()
    assert d.reason.startswith("stale_book")


async def test_gate_06_wide_spread_rejects(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "max_spread_for_entry", 0.04)
    world.book = _book(spread=0.05)
    d = await _check()
    assert d.reason.startswith("spread_too_wide")


async def test_gate_06_low_liquidity_rejects(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "min_liquidity_for_entry_usd", 3000.0)
    world.book = _book(liquidity_num=100.0)
    d = await _check()
    assert d.reason.startswith("liquidity_low")


async def test_gate_06_falls_back_to_volume_when_gamma_omits_liquidity(world, monkeypatch):
    """Sin `liquidity_num`, el proxy es `volume_24hr`. Con ambos nulos, se rechaza."""
    monkeypatch.setattr(engine.settings, "min_liquidity_for_entry_usd", 3000.0)

    world.book = _book(liquidity_num=None, volume_24hr=50_000.0)
    assert (await _check()).accepted is True

    world.book = _book(liquidity_num=None, volume_24hr=100.0)
    assert (await _check()).reason.startswith("liquidity_low")

    world.book = _book(liquidity_num=None, volume_24hr=None)
    assert (await _check()).reason.startswith("liquidity_low")


# ---------------------------------------------------------------------------
# 6.5 Time-to-resolution floor
# ---------------------------------------------------------------------------


async def test_gate_065_rejects_market_about_to_resolve(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "max_time_to_resolution_hours_floor", 2.0)
    world.end_date = datetime.now(UTC) + timedelta(hours=1)
    d = await _check()
    assert d.reason.startswith("too_close_to_resolution")


async def test_gate_065_naive_end_date_is_treated_as_utc(world, monkeypatch):
    """Postgres puede devolver un datetime sin tzinfo. Restarle un aware explota."""
    monkeypatch.setattr(engine.settings, "max_time_to_resolution_hours_floor", 2.0)
    world.end_date = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)
    d = await _check()
    assert d.reason.startswith("too_close_to_resolution")


async def test_gate_065_no_end_date_does_not_block(world):
    world.end_date = None
    assert (await _check()).accepted is True


# ---------------------------------------------------------------------------
# 7. min_edge y Kelly > 0
# ---------------------------------------------------------------------------


async def test_gate_07_edge_below_minimum_rejects(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "min_edge", 0.02)
    d = await _check(edge_value=0.019)
    assert d.reason.startswith("edge 0.0190")


async def test_gate_07_zero_kelly_rejects(world):
    d = await _check(sizing=SizingResult(f_star=0.0, shares=0.0, notional_usd=0.0))
    assert d.reason == "kelly_zero_or_negative"


async def test_gate_075_low_confidence_rejects(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "min_signal_confidence", 0.30)
    assert (await _check(confidence=0.29)).reason.startswith("confidence")
    # `None` significa "la estrategia no reporta confianza", no "confianza cero".
    assert (await _check(confidence=None)).accepted is True


# ---------------------------------------------------------------------------
# 8. max_risk_per_trade_usd — recorta, no rechaza
# ---------------------------------------------------------------------------


async def test_gate_08_caps_notional_and_scales_shares(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "max_risk_per_trade_usd", 50.0)
    d = await _check(sizing=SizingResult(f_star=0.5, shares=400.0, notional_usd=80.0))
    assert d.accepted is True
    assert d.adjusted_notional_usd == pytest.approx(50.0)
    # Las shares se escalan por el mismo ratio: el precio implícito no cambia.
    assert d.adjusted_shares == pytest.approx(400.0 * 50.0 / 80.0)


async def test_gate_08_throttle_applies_before_the_cap(world, monkeypatch):
    """kappa multiplica primero; el cap se aplica sobre el nocional ya reducido."""
    monkeypatch.setattr(engine.settings, "dd_throttle_pct", 0.10)
    monkeypatch.setattr(engine.settings, "dd_halt_pct", 0.15)
    monkeypatch.setattr(engine.settings, "max_risk_per_trade_usd", 50.0)
    world.drawdown = -0.12
    # 80 * 0.5 = 40, por debajo del cap: el cap no toca nada.
    d = await _check(sizing=SizingResult(f_star=0.5, shares=400.0, notional_usd=80.0))
    assert d.adjusted_notional_usd == pytest.approx(40.0)
    assert d.adjusted_shares == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# 9. max_exposure_per_market_usd — incluye la posición ya abierta
# ---------------------------------------------------------------------------


async def test_gate_09_market_exposure_full_rejects(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "max_exposure_per_market_usd", 200.0)
    world.market_cost = 200.0
    d = await _check()
    assert d.reason.startswith("market_exposure_full")


async def test_gate_09_partial_room_clips_to_the_room(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "max_exposure_per_market_usd", 200.0)
    world.market_cost = 190.0
    d = await _check()  # nocional 20, hueco 10
    assert d.accepted is True
    assert d.adjusted_notional_usd == pytest.approx(10.0)
    assert d.adjusted_shares == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# 10. Gross exposure cap del portfolio
# ---------------------------------------------------------------------------


async def test_gate_10_gross_exposure_full_rejects(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "bankroll_usd", 1000.0)
    monkeypatch.setattr(engine.settings, "max_gross_exposure_pct", 0.50)
    world.gross = 500.0
    d = await _check()
    assert d.reason.startswith("gross_exposure_full")


async def test_gate_10_partial_room_clips_to_the_room(world, monkeypatch):
    monkeypatch.setattr(engine.settings, "bankroll_usd", 1000.0)
    monkeypatch.setattr(engine.settings, "max_gross_exposure_pct", 0.50)
    monkeypatch.setattr(engine.settings, "max_exposure_per_market_usd", 10_000.0)
    world.gross = 495.0  # hueco de 5
    d = await _check()
    assert d.accepted is True
    assert d.adjusted_notional_usd == pytest.approx(5.0)
    assert d.adjusted_shares == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# 11. Cash reserve
# ---------------------------------------------------------------------------


def _loosen_upstream_caps(monkeypatch):
    """Abre las compuertas 8-10 para que la 11 sea la que muerda."""
    monkeypatch.setattr(engine.settings, "bankroll_usd", 1000.0)
    monkeypatch.setattr(engine.settings, "max_risk_per_trade_usd", 10_000.0)
    monkeypatch.setattr(engine.settings, "max_exposure_per_market_usd", 10_000.0)
    monkeypatch.setattr(engine.settings, "max_gross_exposure_pct", 10.0)
    monkeypatch.setattr(engine.settings, "min_cash_reserve_pct", 0.10)


async def test_gate_11_clips_to_respect_the_cash_reserve(world, monkeypatch):
    _loosen_upstream_caps(monkeypatch)
    # cash_now = 1000. Reserva mínima 100. Pedimos 950 → permitido 900.
    d = await _check(sizing=SizingResult(f_star=0.9, shares=1000.0, notional_usd=950.0))
    assert d.accepted is True
    assert d.adjusted_notional_usd == pytest.approx(900.0)
    assert d.adjusted_shares == pytest.approx(1000.0 * 900.0 / 950.0)


async def test_gate_11_no_room_left_rejects(world, monkeypatch):
    _loosen_upstream_caps(monkeypatch)
    world.gross = 900.0  # cash_now = 1000 + 0 - 900 = 100 == la reserva
    d = await _check(sizing=SizingResult(f_star=0.9, shares=1000.0, notional_usd=950.0))
    assert d.accepted is False
    assert d.reason.startswith("cash_reserve_breach")


async def test_gate_11_realized_pnl_counts_as_cash(world, monkeypatch):
    """Las ganancias realizadas engordan el cash disponible; las pérdidas lo comen."""
    _loosen_upstream_caps(monkeypatch)
    world.gross = 900.0
    world.realized = 500.0  # cash_now = 600
    d = await _check(sizing=SizingResult(f_star=0.9, shares=1000.0, notional_usd=950.0))
    assert d.accepted is True
    assert d.adjusted_notional_usd == pytest.approx(500.0)  # 600 - 100 de reserva
