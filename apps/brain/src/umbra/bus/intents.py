"""El productor de `nocti:intents`. La mitad izquierda de la Fase 3.

brain pide, exec responde. En `shadow` nadie firma: exec cotiza el intent contra
el libro real y devuelve el fill que **se habría** obtenido. Este módulo escribe
lo que se pidió; `bus/fills.py` recoge lo que pasó.

UN OUTBOX, NO UN `XADD` SUELTO
------------------------------
Postgres y Redis no comparten transacción, así que hay que elegir cuál de las dos
mentiras se prefiere:

  - Publicar antes de commitear: exec cotiza —y en la Fase 4, **firma**— un intent
    cuya fila puede desaparecer si la transacción de la señal se revierte. Queda
    una orden sin registro. Es la mentira cara.
  - Commitear y publicar después: si el proceso muere en medio, hay una fila que
    nadie envió. Es la mentira barata: la fila está ahí, con su `expires_at`, y el
    barrido siguiente la publica o la marca `EXPIRED`.

Se elige la segunda. La tabla `intents` es el outbox: `stage_intent` escribe la
fila dentro de la transacción de la señal, y `publish_pending` la envía cuando esa
transacción ya está en disco.

De ahí sale un productor **al menos una vez**: si el proceso muere entre el `XADD`
y el `UPDATE published_at`, el intent se reenvía. Que eso sea correcto y no una
orden duplicada depende enteramente de la regla de idempotencia del contrato
(§3.3): exec hace `SET nocti:intent:{id} … NX` antes de tocar nada, y a un intent
repetido le re-emite el fill que ya calculó. Esa regla no es una defensa contra un
bug improbable — es la mitad que hace correcta a esta.

LO QUE ESTE MÓDULO NO HACE
--------------------------
No dimensiona. `size_usd` es el nocional que `risk/engine.py` ya firmó y que
`signals.notional_usd` guarda; aquí solo se copia. Si algún día se «ajusta» un
tamaño en este archivo, el presupuesto único de capital habrá dejado de existir
sin que ningún test se entere.

No emite cierres. `execute_close` sigue siendo cosa de `exit_engine`, y medir el
slippage de una venta contra el libro real es la Fase 3 bis. `action` existe en la
fila para que ese día no haya que migrar nada.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.bus.contract import (
    INTENTS_STREAM,
    ContractError,
    format_decimal,
    format_price,
    strategy_from_edge_name,
    validate_intent_fields,
)
from umbra.bus.tokens import token_for_side
from umbra.cache.redis_client import get_redis
from umbra.config import settings
from umbra.db.models import Intent, Market, Signal
from umbra.execution.paper import compute_fill_price, theoretical_price
from umbra.logging import get_logger

log = get_logger("umbra.bus.intents")

# El stream no se poda solo. `~` deja que Redis pode cuando le venga bien.
INTENTS_MAXLEN = 100_000

# Las escalas de las columnas en `db/models.py`.
_PRICE = Decimal("0.000001")  # Numeric(12, 6)
_MONEY = Decimal("0.000001")  # Numeric(20, 6)
_BPS_DENOM = Decimal("10000")
_ZERO = Decimal("0")
_ONE = Decimal("1")

# `IOC`, y no `GTC` ni `FOK`.
#
# La pregunta que la Fase 3 hace es «¿qué me habría dado el libro AHORA?». `GTC`
# describe una orden que descansa y se llena con el flujo que venga después: eso
# no está en la foto, y `quote.ts` lo cotiza como IOC de todos modos. `FOK`
# rechazaría cualquier libro que no llene el nocional entero, y tirar la muestra
# de los libros finos es tirar precisamente los casos que queremos medir.
#
# `IOC` llena lo que hay y reporta `PARTIAL` con su slippage. Es el que más
# información devuelve por intent.
INTENT_TIF = "IOC"

# El modo del bus cuando brain corre en `shadow`. El camino `live` se inyecta en
# la Fase 4, y hasta entonces exec rechaza cualquier intent que lo pida.
SHADOW = "shadow"


@dataclass(frozen=True)
class PublishStats:
    """Lo que hizo un barrido del outbox."""

    published: int = 0
    expired: int = 0
    invalid: int = 0
    # `True` si el barrido se cortó porque Redis no respondía. El backlog sigue ahí.
    redis_down: bool = False


def _q(x: Decimal, exp: Decimal, rounding: str = ROUND_HALF_UP) -> Decimal:
    return x.quantize(exp, rounding=rounding)


def _clamp(x: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return max(lo, min(hi, x))


def limit_price_for(side: str, mid_yes: Decimal, max_slippage_bps: int) -> Decimal:
    """Hasta dónde puede caminar exec el libro comprando este token.

    No es el precio que brain predice, es el peor que aceptaría. Poner aquí la
    predicción (`compute_fill_price`) sería una compuerta que se cierra justo
    donde el modelo dice que estará el precio: cualquier libro un poco peor
    volvería `PARTIAL`, y en vez de medir cuánto se equivocó el modelo mediríamos
    con qué frecuencia se equivoca. El sesgo apuntaría al lado que halaga.

    Así que el límite se pone en la tolerancia declarada, y quien decide es la
    compuerta de slippage de `quote.ts`, que además conserva la medición cuando
    rechaza. Los dos números dicen lo mismo desde dos sitios: el límite corta por
    precio absoluto, la compuerta por distancia al mid.

    Redondeo a la baja: comprando, un límite más bajo es el lado conservador.
    """
    theoretical = theoretical_price(side, mid_yes)
    tolerance = _ONE + (Decimal(max_slippage_bps) / _BPS_DENOM)
    return _clamp(_q(theoretical * tolerance, _PRICE, ROUND_DOWN), _ZERO, _ONE)


def intent_to_fields(intent: Intent) -> dict[str, str]:
    """La fila del outbox → los campos planos que van al stream.

    Los opcionales que valen `None` sencillamente no se escriben: exec los lee
    como ausentes. Un `""` en `signal_id` sería un entero vacío, y el parser de
    allí tendría que decidir qué significa. No tiene por qué.
    """
    fields = {
        "intent_id": intent.intent_id,
        "ts": intent.ts.isoformat(),
        "strategy": intent.strategy,
        "mode": intent.mode,
        "condition_id": intent.market_id,
        "token_id": intent.token_id,
        "side": intent.bus_side,
        "size_usd": format_decimal(intent.size_usd),
        "limit_price": format_price(intent.limit_price),
        "tif": intent.tif,
        "max_slippage_bps": str(intent.max_slippage_bps),
        "expires_at": intent.expires_at.isoformat(),
    }
    if intent.signal_id is not None:
        fields["signal_id"] = str(intent.signal_id)
    if intent.expected_slippage_bps is not None:
        # El cable lleva un entero; la columna guarda los cuatro decimales. El
        # reporte de divergencia lee la columna, no el cable.
        fields["expected_slippage_bps"] = str(
            int(intent.expected_slippage_bps.to_integral_value(ROUND_HALF_UP))
        )
    return fields


# ---------------------------------------------------------------------------
# Escribir en el outbox
# ---------------------------------------------------------------------------


async def stage_intent(
    session: AsyncSession,
    signal: Signal,
    liquidity_usd: float | None,
    now: datetime | None = None,
) -> Intent | None:
    """Deja el intent en el outbox, dentro de la transacción de la señal.

    No publica nada. Devuelve `None` —sin levantar— cuando el intent no se puede
    construir con honestidad: sin token identificable, sin nocional firmado, sin
    una estrategia que el bus reconozca. Un intent a medias no se manda: la señal,
    el paper fill y la auditoría siguen su camino, y en `intents` no queda una fila
    que el reporte tendría que aprender a ignorar.

    `liquidity_usd` es el mismo que recibe `execution/paper.py`. Tiene que serlo:
    `expected_slippage_bps` es lo que ese modelo predijo, no una segunda opinión
    calculada aquí con otros datos. La resta de la Fase 3 solo significa algo si
    la mitad izquierda es exactamente la que el backtest usó.
    """
    now = now or datetime.now(UTC)

    if not signal.accepted or signal.notional_usd is None or signal.notional_usd <= 0:
        return None
    if signal.market_price is None:
        return None

    try:
        strategy = strategy_from_edge_name(signal.edge_name)
    except ContractError as exc:
        log.warning("intents.sin_estrategia", signal_id=signal.id, error=str(exc))
        return None

    market = await session.get(Market, signal.market_id)
    if market is None:  # pragma: no cover — la FK de `signals` ya lo garantiza
        log.warning("intents.mercado_desconocido", market_id=signal.market_id)
        return None

    token_id = token_for_side(market.outcomes, market.clob_token_ids, signal.side)
    if token_id is None:
        # Preferimos no medir a medir el token equivocado: el slippage del libro
        # del NO no dice nada sobre una compra de YES.
        log.warning(
            "intents.token_no_resoluble",
            market_id=signal.market_id,
            side=signal.side,
            outcomes=list(market.outcomes or []),
        )
        return None

    mid_yes = _q(signal.market_price, _PRICE)
    notional = _q(signal.notional_usd, _MONEY)
    max_bps = settings.intent_max_slippage_bps

    # El mismo modelo, la misma liquidez, el mismo nocional que el paper fill.
    _, expected_bps = compute_fill_price(signal.side, mid_yes, notional, liquidity_usd)

    intent = Intent(
        intent_id=str(uuid.uuid4()),
        # Explícito, no `server_default`: el `ts` que viaja en el cable tiene que
        # ser el mismo que el de la fila, y el de la base no existe hasta el flush.
        ts=now,
        signal_id=signal.id,
        market_id=signal.market_id,
        strategy=strategy,
        mode=SHADOW,
        token_id=token_id,
        side=signal.side,
        action="OPEN",
        bus_side="BUY",  # abrir una posición es comprar su token, siempre.
        size_usd=notional,
        limit_price=limit_price_for(signal.side, mid_yes, max_bps),
        tif=INTENT_TIF,
        max_slippage_bps=max_bps,
        expires_at=now + timedelta(seconds=settings.intent_ttl_sec),
        expected_slippage_bps=expected_bps,
        published_at=None,
        status=None,
    )

    # Se valida aquí, con un `signal_id` y un stack trace a mano, y no dentro de
    # exec, donde un intent malformado acaba en `nocti:intents:dead` y brain se
    # queda esperando un fill que nunca llega.
    try:
        validate_intent_fields(intent_to_fields(intent))
    except ContractError as exc:
        log.warning("intents.no_valida", signal_id=signal.id, error=str(exc))
        return None

    session.add(intent)
    log.info(
        "intents.staged",
        intent_id=intent.intent_id,
        signal_id=signal.id,
        market_id=signal.market_id,
        strategy=strategy,
        size_usd=float(notional),
        expected_slippage_bps=float(expected_bps),
    )
    return intent


# ---------------------------------------------------------------------------
# Drenar el outbox
# ---------------------------------------------------------------------------


async def publish_pending(
    session: AsyncSession,
    now: datetime | None = None,
    batch: int | None = None,
) -> PublishStats:
    """Publica el backlog del outbox. Se llama **después** del commit de la señal.

    `FOR UPDATE SKIP LOCKED` porque hoy solo hay un brain, y el día que haya dos
    quiero que el segundo se salte las filas del primero en vez de publicarlas
    otra vez. Cuesta nada y evita una clase entera de bugs.

    Si Redis no responde, el barrido se corta y no se marca nada: las filas siguen
    en el backlog y el tick siguiente lo intenta. No hay nada que reparar a mano,
    y no se pierde ningún intent que no haya muerto de viejo.
    """
    now = now or datetime.now(UTC)
    limit = batch or settings.intent_publish_batch

    stmt = (
        select(Intent)
        .where(Intent.published_at.is_(None), Intent.status.is_(None))
        .order_by(Intent.ts)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    if not rows:
        return PublishStats()

    redis = get_redis()
    published = expired = invalid = 0
    redis_down = False

    for row in rows:
        if row.expires_at <= now:
            # Murió en el outbox: el proceso estuvo caído más que su TTL, o Redis
            # lo estuvo. Queda escrito que se pidió y que nunca se preguntó.
            row.status = "EXPIRED"
            row.resolved_at = now
            row.error = "expiró en el outbox: no se publicó antes de expires_at"
            expired += 1
            continue

        try:
            fields = intent_to_fields(row)
            validate_intent_fields(fields)
        except ContractError as exc:
            # No debería pasar: `stage_intent` ya validó. Si pasa, alguien tocó la
            # fila por debajo. No se publica, y se dice por qué.
            row.status = "ERROR"
            row.resolved_at = now
            row.error = f"no cumple el contrato al publicar: {exc}"
            invalid += 1
            log.error("intents.corrupto", intent_id=row.intent_id, error=str(exc))
            continue

        try:
            await redis.xadd(INTENTS_STREAM, fields, maxlen=INTENTS_MAXLEN, approximate=True)
        except Exception as exc:
            # Si Redis no contesta para éste, no contestará para los siguientes.
            # Se corta y se conserva lo ya marcado.
            log.warning("intents.publish_failed", intent_id=row.intent_id, error=repr(exc))
            redis_down = True
            break

        row.published_at = now
        published += 1

    await session.commit()

    if published or expired or invalid:
        log.info(
            "intents.publish",
            published=published,
            expired=expired,
            invalid=invalid,
            backlog=len(rows),
        )
    return PublishStats(
        published=published, expired=expired, invalid=invalid, redis_down=redis_down
    )
