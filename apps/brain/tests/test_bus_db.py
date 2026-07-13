"""El bus contra Postgres y Redis de verdad: outbox, consumidor y reporte.

Estos tests recorren el camino entero de la Fase 3 sin `exec`: brain deja un intent
en el outbox, lo publica, y se le devuelve el fill que exec habría devuelto. Lo que
se comprueba no es que las funciones corran, sino las cuatro promesas del diseño:

  1. Nada se publica antes de estar en disco (`published_at` es el testigo).
  2. Una fila `shadow` no mueve ni un centavo de la contabilidad.
  3. Un fill repetido no se escribe dos veces, y lo impone Postgres.
  4. Un intent sin respuesta no desaparece: queda para el reporte.

El `condition_id` es hex de 64 caracteres, y no `0xtest_...` como en los demás
tests, porque el contrato del bus lo exige y `stage_intent` lo valida antes de
escribir. Por eso estos tests limpian sus propias filas en vez de apoyarse en el
purgado automático de `conftest`, que busca el prefijo `0xtest_`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from umbra.analytics.shadow_divergence import build_report, load_samples
from umbra.bus.contract import INTENTS_STREAM, FillMessage, validate_intent_fields
from umbra.bus.fills import FillOutcome, apply_fill
from umbra.bus.intents import publish_pending, stage_intent
from umbra.cache.redis_client import get_redis
from umbra.config import settings
from umbra.db.models import Fill, Intent, Market, PaperPosition, Signal
from umbra.db.session import get_sessionmaker

CID = "0x" + "ab" * 32
OTHER_CID = "0x" + "cd" * 32


@pytest_asyncio.fixture
async def sm(monkeypatch):
    """Sesión limpia y un modelo de slippage clavado.

    Los umbrales van fijados aquí: el suite no puede depender del `.env` del
    operador. Con `liquidity=5000` y `notional=100`, el ratio es 0.02 y el modelo
    predice `20 + 200*0.02 = 24` bps.
    """
    monkeypatch.setattr(settings, "slippage_base_bps", 20.0)
    monkeypatch.setattr(settings, "slippage_size_factor_bps", 200.0)
    monkeypatch.setattr(settings, "slippage_cap_bps", 500.0)
    monkeypatch.setattr(settings, "fee_bps", 0.0)
    monkeypatch.setattr(settings, "intent_max_slippage_bps", 500)
    monkeypatch.setattr(settings, "intent_ttl_sec", 60)
    monkeypatch.setattr(settings, "intent_publish_batch", 100)

    maker = get_sessionmaker()
    await _wipe(maker)
    yield maker
    await _wipe(maker)


async def _wipe(maker) -> None:
    """`conftest` exige que la base se llame `*test*`; borrar `intents` entero es seguro."""
    async with maker() as session:
        await session.execute(delete(Intent))
        await session.execute(delete(Fill).where(Fill.market_id.in_([CID, OTHER_CID])))
        await session.execute(
            delete(PaperPosition).where(PaperPosition.market_id.in_([CID, OTHER_CID]))
        )
        await session.execute(delete(Signal).where(Signal.market_id.in_([CID, OTHER_CID])))
        await session.execute(delete(Market).where(Market.condition_id.in_([CID, OTHER_CID])))
        await session.commit()
    await _drain_our_stream_entries()


async def _drain_our_stream_entries() -> None:
    """Borra del stream sólo lo que estos tests publicaron.

    Un `DEL nocti:intents` sería más corto y tiraría el backlog de un exec real si
    alguien corre los tests apuntando al Redis de operación. Se borran por id.
    """
    redis = get_redis()
    try:
        entries = await redis.xrange(INTENTS_STREAM, "-", "+")
    except Exception:
        return
    ids = [eid for eid, fields in entries if fields.get("condition_id") in {CID, OTHER_CID}]
    if ids:
        await redis.xdel(INTENTS_STREAM, *ids)


async def _stream_entries_for(condition_id: str) -> list[dict[str, str]]:
    redis = get_redis()
    entries = await redis.xrange(INTENTS_STREAM, "-", "+")
    return [f for _eid, f in entries if f.get("condition_id") == condition_id]


async def _seed_market(session, condition_id: str = CID, outcomes=("Yes", "No")) -> None:
    session.add(
        Market(
            condition_id=condition_id,
            gamma_id=f"gid_{condition_id[-6:]}",
            slug=f"slug-{condition_id[-6:]}",
            question="¿Test del bus?",
            clob_token_ids=["tok_yes", "tok_no"],
            outcomes=list(outcomes),
        )
    )
    await session.flush()


async def _seed_signal(
    session,
    condition_id: str = CID,
    side: str = "BUY_YES",
    edge_name: str = "overreaction_v1",
    notional: str = "100",
    price: str = "0.300000",
) -> Signal:
    signal = Signal(
        ts=datetime.now(UTC),
        market_id=condition_id,
        edge_name=edge_name,
        side=side,
        market_price=Decimal(price),
        fair_price=Decimal("0.350000"),
        edge_value=Decimal("0.050000"),
        strength=Decimal("2.100000"),
        size_shares=Decimal("333.333333"),
        notional_usd=Decimal(notional),
        accepted=True,
        reason="ok",
        mode="shadow",
    )
    session.add(signal)
    await session.flush()
    return signal


def _fill_msg(intent_id: str, **overrides) -> FillMessage:
    """Lo que exec habría publicado. Los defaults describen un llenado entero."""
    defaults = {
        "intent_id": intent_id,
        "ts": datetime.now(UTC).isoformat(),
        "mode": "shadow",
        "status": "FILLED",
        "filled_shares": Decimal("322.580645"),
        "avg_price": Decimal("0.310000"),
        "notional_usd": Decimal("100.000000"),
        "fees_usd": Decimal("0.000000"),
        "order_id": "",
        "tx_hash": "",
        "mid_price": Decimal("0.300000"),
        "expected_slippage_bps": 24,
        "realized_slippage_bps": 333,
        "error": "",
    }
    return FillMessage(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# El outbox: escribir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_intent_escribe_la_fila_sin_publicarla(sm):
    async with sm() as session:
        await _seed_market(session)
        signal = await _seed_signal(session)

        intent = await stage_intent(session, signal, liquidity_usd=5000.0)
        await session.commit()

    assert intent is not None
    # La promesa central del outbox: existe en disco, no ha salido al bus.
    assert intent.published_at is None
    assert intent.status is None

    assert intent.mode == "shadow"
    assert intent.strategy == "overreaction"
    assert intent.token_id == "tok_yes"
    assert intent.side == "BUY_YES"  # el idioma de brain
    assert intent.bus_side == "BUY"  # el del bus
    assert intent.action == "OPEN"
    assert intent.tif == "IOC"
    assert intent.size_usd == Decimal("100.000000")
    # 0.30 * (1 + 500/10000) = 0.315
    assert intent.limit_price == Decimal("0.315000")
    # 20 + 200 * (100/5000) = 24
    assert intent.expected_slippage_bps == Decimal("24.0000")
    assert await _stream_entries_for(CID) == []


@pytest.mark.asyncio
async def test_stage_intent_compra_el_token_no_al_comprar_no(sm):
    async with sm() as session:
        await _seed_market(session)
        signal = await _seed_signal(session, side="BUY_NO")
        intent = await stage_intent(session, signal, liquidity_usd=5000.0)
        await session.commit()

    assert intent.token_id == "tok_no"
    # (1 - 0.30) * 1.05 = 0.735
    assert intent.limit_price == Decimal("0.735000")


@pytest.mark.asyncio
async def test_stage_intent_no_emite_si_no_sabe_que_token_comprar(sm):
    """Preferimos no medir a medir el libro del token equivocado."""
    async with sm() as session:
        await _seed_market(session, outcomes=("Trump", "Biden"))
        signal = await _seed_signal(session)

        assert await stage_intent(session, signal, liquidity_usd=5000.0) is None
        await session.commit()

    async with sm() as session:
        assert (await session.execute(select(Intent))).scalars().all() == []


@pytest.mark.asyncio
async def test_stage_intent_no_emite_para_un_edge_que_el_bus_no_conoce(sm):
    """Un edge sin estrategia se colaría en el presupuesto de capital de otra."""
    async with sm() as session:
        await _seed_market(session)
        signal = await _seed_signal(session, edge_name="liquidity_vacuum_v1")

        assert await stage_intent(session, signal, liquidity_usd=5000.0) is None
        await session.commit()


@pytest.mark.asyncio
async def test_stage_intent_ignora_una_senal_rechazada(sm):
    async with sm() as session:
        await _seed_market(session)
        signal = await _seed_signal(session)
        signal.accepted = False

        assert await stage_intent(session, signal, liquidity_usd=5000.0) is None


# ---------------------------------------------------------------------------
# El outbox: drenar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_pending_publica_y_marca(sm):
    async with sm() as session:
        await _seed_market(session)
        signal = await _seed_signal(session)
        intent = await stage_intent(session, signal, liquidity_usd=5000.0)
        intent_id = intent.intent_id
        await session.commit()

    async with sm() as session:
        stats = await publish_pending(session)
    assert stats.published == 1
    assert not stats.redis_down

    entries = await _stream_entries_for(CID)
    assert len(entries) == 1
    fields = entries[0]
    validate_intent_fields(fields)
    assert fields["intent_id"] == intent_id
    assert fields["side"] == "BUY"
    assert fields["token_id"] == "tok_yes"
    assert fields["size_usd"] == "100.000000"
    assert fields["limit_price"] == "0.315000"
    assert fields["expected_slippage_bps"] == "24"

    async with sm() as session:
        row = await session.get(Intent, intent_id)
        assert row.published_at is not None
        assert row.status is None  # publicado, todavía sin respuesta


@pytest.mark.asyncio
async def test_publish_pending_es_idempotente_por_published_at(sm):
    """El segundo barrido no reenvía lo que ya salió."""
    async with sm() as session:
        await _seed_market(session)
        signal = await _seed_signal(session)
        await stage_intent(session, signal, liquidity_usd=5000.0)
        await session.commit()

    async with sm() as session:
        assert (await publish_pending(session)).published == 1
    async with sm() as session:
        assert (await publish_pending(session)).published == 0

    assert len(await _stream_entries_for(CID)) == 1


@pytest.mark.asyncio
async def test_publish_pending_no_publica_un_intent_que_expiro_en_el_outbox(sm):
    """El proceso estuvo caído más que el TTL. Queda escrito que se pidió."""
    async with sm() as session:
        await _seed_market(session)
        signal = await _seed_signal(session)
        intent = await stage_intent(session, signal, liquidity_usd=5000.0)
        intent.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        intent_id = intent.intent_id
        await session.commit()

    async with sm() as session:
        stats = await publish_pending(session)
    assert stats.published == 0
    assert stats.expired == 1

    assert await _stream_entries_for(CID) == []
    async with sm() as session:
        row = await session.get(Intent, intent_id)
        assert row.status == "EXPIRED"
        assert row.published_at is None
        assert "outbox" in row.error
        assert row.resolved_at is not None


@pytest.mark.asyncio
async def test_publish_pending_arrastra_el_backlog_de_un_tick_anterior(sm):
    """Un intent que quedó sin publicar no espera a que llegue otra señal."""
    async with sm() as session:
        await _seed_market(session)
        for _ in range(3):
            signal = await _seed_signal(session)
            await stage_intent(session, signal, liquidity_usd=5000.0)
        await session.commit()

    async with sm() as session:
        assert (await publish_pending(session)).published == 3
    assert len(await _stream_entries_for(CID)) == 3


# ---------------------------------------------------------------------------
# El consumidor
# ---------------------------------------------------------------------------


async def _staged_intent(sm, **signal_kwargs) -> str:
    async with sm() as session:
        await _seed_market(session)
        signal = await _seed_signal(session, **signal_kwargs)
        intent = await stage_intent(session, signal, liquidity_usd=5000.0)
        intent_id = intent.intent_id
        await session.commit()
    return intent_id


@pytest.mark.asyncio
async def test_apply_fill_escribe_la_medicion_y_resuelve_el_intent(sm):
    intent_id = await _staged_intent(sm)

    async with sm() as session:
        assert await apply_fill(session, _fill_msg(intent_id)) is FillOutcome.WRITTEN

    async with sm() as session:
        fill = (
            await session.execute(select(Fill).where(Fill.intent_id == intent_id))
        ).scalar_one()
        # Las dos marcas: `mode` dice de dónde viene, `action` la saca de las sumas.
        assert fill.mode == "shadow"
        assert fill.action == "SHADOW"
        assert fill.market_id == CID
        assert fill.side == "BUY_YES"
        assert fill.fill_price == Decimal("0.310000")
        assert fill.mid_at_fill == Decimal("0.300000")
        assert fill.slippage_bps == Decimal("333.0000")
        # Una medición no realiza nada.
        assert fill.realized_pnl_usd == Decimal("0")
        assert fill.order_id is None and fill.tx_hash is None

        intent = await session.get(Intent, intent_id)
        assert intent.status == "FILLED"
        assert intent.resolved_at is not None
        assert intent.error is None


@pytest.mark.asyncio
async def test_una_fila_shadow_no_mueve_ninguna_posicion(sm):
    """Si algún día suma, el paper trading contará cada operación dos veces."""
    intent_id = await _staged_intent(sm)

    async with sm() as session:
        await apply_fill(session, _fill_msg(intent_id))

    async with sm() as session:
        posiciones = (
            await session.execute(select(PaperPosition).where(PaperPosition.market_id == CID))
        ).scalars().all()
        assert posiciones == []


@pytest.mark.asyncio
async def test_un_fill_repetido_no_se_escribe_dos_veces(sm):
    intent_id = await _staged_intent(sm)

    async with sm() as session:
        assert await apply_fill(session, _fill_msg(intent_id)) is FillOutcome.WRITTEN
    # exec re-emite el fill que ya calculó cuando brain reenvía el intent.
    async with sm() as session:
        assert await apply_fill(session, _fill_msg(intent_id)) is FillOutcome.DUPLICATE

    async with sm() as session:
        fills = (
            await session.execute(select(Fill).where(Fill.intent_id == intent_id))
        ).scalars().all()
        assert len(fills) == 1


@pytest.mark.asyncio
async def test_la_idempotencia_la_impone_postgres_no_el_consumidor(sm):
    """Si el intent aparece sin resolver pero el fill ya está, manda el índice único."""
    intent_id = await _staged_intent(sm)

    async with sm() as session:
        await apply_fill(session, _fill_msg(intent_id))

    # Se rebobina el intent a «en vuelo»: el guard de `status` ya no protege.
    async with sm() as session:
        intent = await session.get(Intent, intent_id)
        intent.status = None
        intent.resolved_at = None
        await session.commit()

    async with sm() as session:
        assert await apply_fill(session, _fill_msg(intent_id)) is FillOutcome.DUPLICATE

    async with sm() as session:
        fills = (
            await session.execute(select(Fill).where(Fill.intent_id == intent_id))
        ).scalars().all()
        assert len(fills) == 1


@pytest.mark.asyncio
async def test_un_fill_sin_intent_no_se_escribe(sm):
    """`market_id` es NOT NULL y el bus no lo lleva. Se aparta, no se inventa."""
    huerfano = "99999999-9999-4999-8999-999999999999"
    async with sm() as session:
        assert await apply_fill(session, _fill_msg(huerfano)) is FillOutcome.ORPHAN

    async with sm() as session:
        assert (await session.execute(select(Fill))).scalars().all() == []


@pytest.mark.asyncio
async def test_un_fill_live_no_se_contabiliza_en_la_fase_3(sm):
    """Una orden firmada mueve posiciones. Escribirla con action='SHADOW' la borraría."""
    intent_id = await _staged_intent(sm)

    async with sm() as session:
        outcome = await apply_fill(session, _fill_msg(intent_id, mode="live"))
        assert outcome is FillOutcome.NOT_SHADOW

    async with sm() as session:
        assert (await session.execute(select(Fill))).scalars().all() == []
        assert (await session.get(Intent, intent_id)).status is None


@pytest.mark.asyncio
async def test_un_rechazo_escribe_una_fila_terminal_sin_precio_inventado(sm):
    """Sin libro no hay mid. Un cero ahí sería un precio que nadie vio."""
    intent_id = await _staged_intent(sm)

    msg = _fill_msg(
        intent_id,
        status="REJECTED",
        filled_shares=Decimal("0"),
        avg_price=Decimal("0"),
        notional_usd=Decimal("0"),
        mid_price=None,
        realized_slippage_bps=None,
        error="sin mid: el libro está vacío, cruzado o tiene un solo lado",
    )
    async with sm() as session:
        assert await apply_fill(session, msg) is FillOutcome.WRITTEN

    async with sm() as session:
        fill = (
            await session.execute(select(Fill).where(Fill.intent_id == intent_id))
        ).scalar_one()
        assert fill.status == "REJECTED"
        assert fill.mid_at_fill is None
        assert fill.slippage_bps is None
        assert fill.shares == Decimal("0")

        intent = await session.get(Intent, intent_id)
        assert intent.status == "REJECTED"
        assert intent.error.startswith("sin mid")


@pytest.mark.asyncio
async def test_un_rechazo_por_slippage_conserva_la_medicion(sm):
    """Es el dato que la fase vino a buscar: el libro habría costado 520bps."""
    intent_id = await _staged_intent(sm)

    msg = _fill_msg(
        intent_id,
        status="REJECTED",
        filled_shares=Decimal("0"),
        avg_price=Decimal("0"),
        notional_usd=Decimal("0"),
        realized_slippage_bps=520,
        error="slippage 520bps > max_slippage_bps 500",
    )
    async with sm() as session:
        await apply_fill(session, msg)

    async with sm() as session:
        fill = (
            await session.execute(select(Fill).where(Fill.intent_id == intent_id))
        ).scalar_one()
        assert fill.slippage_bps == Decimal("520.0000")
        assert fill.notional_usd == Decimal("0")


# ---------------------------------------------------------------------------
# `shadow` es un modo del bus, no del libro mayor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_un_paper_fill_en_modo_shadow_no_se_sella_como_shadow(sm):
    """Estos fills SÍ son contabilidad: mueven la posición y realizan PnL.

    `signals.mode` vale `shadow` porque el bus está midiendo, pero el fill es un
    paper fill. Si heredara ese `mode`, `risk/engine.last_close_ts_for_market`
    —que filtra `mode != 'shadow'`— dejaría de ver los cierres, y brain reentraría
    en mercados que acaba de cerrar.
    """
    from umbra.execution.paper import execute_signal

    async with sm() as session:
        await _seed_market(session)
        signal = await _seed_signal(session)
        assert signal.mode == "shadow"

        assert await execute_signal(session, signal, liquidity_usd=5000.0) is not None
        await session.commit()

    async with sm() as session:
        fill = (
            await session.execute(select(Fill).where(Fill.market_id == CID))
        ).scalar_one()
        assert fill.mode == "paper"
        assert fill.action == "OPEN"

        # Y la posición sí se movió: esto es contabilidad.
        posicion = (
            await session.execute(select(PaperPosition).where(PaperPosition.market_id == CID))
        ).scalar_one()
        assert posicion.shares == fill.shares


# ---------------------------------------------------------------------------
# El reporte, contra la base
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_el_reporte_resta_lo_predicho_de_lo_medido(sm):
    intent_id = await _staged_intent(sm)
    async with sm() as session:
        await apply_fill(session, _fill_msg(intent_id, realized_slippage_bps=333))

    async with sm() as session:
        muestras = await load_samples(session, since=datetime.now(UTC) - timedelta(hours=1))

    assert len(muestras) == 1
    report = build_report(muestras, datetime.now(UTC) - timedelta(hours=1), datetime.now(UTC))
    # Predijo 24, costó 333.
    assert report.n_measurable == 1
    assert report.overall.expected_mean == 24.0
    assert report.overall.realized_mean == 333.0
    assert report.overall.divergence_mean == 309.0
    assert report.by_strategy["overreaction"].n == 1


@pytest.mark.asyncio
async def test_el_reporte_no_pierde_un_intent_sin_respuesta(sm):
    """Con un JOIN interno, el silencio se confundiría con «no hubo señal»."""
    await _staged_intent(sm)  # se queda en vuelo: nadie le aplica un fill

    async with sm() as session:
        muestras = await load_samples(session, since=datetime.now(UTC) - timedelta(hours=1))

    assert len(muestras) == 1
    assert muestras[0].status is None
    assert muestras[0].realized_bps is None
    assert not muestras[0].measurable

    report = build_report(muestras, datetime.now(UTC) - timedelta(hours=1), datetime.now(UTC))
    assert report.n_intents == 1
    assert report.n_measurable == 0
    assert report.status_counts == {"SIN_RESPUESTA": 1}


# ---------------------------------------------------------------------------
# El camino entero
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_camino_completo_outbox_bus_fill_reporte(sm):
    """stage → publish → (exec) → apply → reporte, sin que nada toque la contabilidad."""
    async with sm() as session:
        await _seed_market(session)
        signal = await _seed_signal(session)
        intent = await stage_intent(session, signal, liquidity_usd=5000.0)
        intent_id = intent.intent_id
        await session.commit()

    async with sm() as session:
        assert (await publish_pending(session)).published == 1

    # Lo que exec habría leído del stream.
    fields = (await _stream_entries_for(CID))[0]
    validate_intent_fields(fields)
    assert fields["intent_id"] == intent_id

    # Lo que exec habría devuelto tras caminar el libro.
    async with sm() as session:
        assert await apply_fill(session, _fill_msg(intent_id)) is FillOutcome.WRITTEN

    async with sm() as session:
        muestras = await load_samples(session, since=datetime.now(UTC) - timedelta(hours=1))
        posiciones = (
            await session.execute(select(PaperPosition).where(PaperPosition.market_id == CID))
        ).scalars().all()

    assert len(muestras) == 1 and muestras[0].measurable
    # La medida entró; la contabilidad no se movió.
    assert posiciones == []
