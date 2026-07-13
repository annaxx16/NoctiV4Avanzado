"""El consumidor de `nocti:fills`. La mitad derecha de la Fase 3.

exec cotizó el intent contra el libro real y publicó lo que **se habría** llenado.
Aquí se guarda, junto al intent que lo pidió. La resta entre `intents.expected_slippage_bps`
y `fills.slippage_bps` es el entregable de la fase: cuánto miente el backtest.

ESTAS FILAS NO SON CONTABILIDAD
------------------------------
Un fill con `mode='shadow'` es un instrumento de medida. No mueve `PaperPosition`,
no realiza PnL, no entra en ninguna exposición ni en ninguna racha. Este módulo
**no llama a `_upsert_open` ni a `_apply_close`**, y no debe. Lleva `action='SHADOW'`
además de `mode='shadow'` para que las consultas viejas —las que filtran
`OPEN`/`CLOSE`— lo ignoren sin que nadie tenga que acordarse.

Si algún día alguien hace que estas filas sumen, el paper trading empezará a
contar cada operación dos veces y el equity será el doble del real.

IDEMPOTENCIA: LA IMPONE POSTGRES
--------------------------------
`fills.intent_id` es `UNIQUE`. Un fill re-emitido por exec —porque brain reenvió
el intent, porque el proceso murió antes del `XACK`— choca contra ese índice y se
descarta. El consumidor no lleva ninguna memoria propia de lo que ya vio, porque
esa memoria se pierde en un restart y el índice no.

El orden es: escribir en Postgres, y solo entonces `XACK`. Si el proceso muere en
medio, el mensaje vuelve por `XAUTOCLAIM` y el segundo intento choca con el índice.
Al revés —ackear y luego escribir— se perdería el fill para siempre. Entrega al
menos una vez, escritura idempotente.

UN FILL SIN INTENT NO SE ESCRIBE
--------------------------------
`fills.market_id` es `NOT NULL` y el mensaje del bus no lo lleva: el bus habla de
`token_id`. El `market_id`, el `side` y el `signal_id` salen de la fila de `intents`.
Sin ella no se puede escribir el fill sin inventarse tres columnas, así que el
mensaje va al dead letter, íntegro, y alguien lo mira. No se pierde: se aparta.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from redis.exceptions import ResponseError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from umbra.bus.contract import (
    BRAIN_GROUP,
    FILLS_STREAM,
    ContractError,
    FillMessage,
    fields_from_entry,
    parse_fill,
)
from umbra.cache.redis_client import get_redis
from umbra.db.models import SHADOW_ACTION, SHADOW_MODE, Fill, Intent
from umbra.db.session import get_sessionmaker
from umbra.logging import get_logger

log = get_logger("umbra.bus.fills")

DEAD_LETTER_STREAM = "nocti:fills:dead"
DEAD_LETTER_MAXLEN = 10_000

# `get_redis()` abre el cliente con `socket_timeout=15`. Un `BLOCK` más largo que
# eso lo mata el socket, no Redis. Cinco segundos deja margen de sobra y hace que
# `stop` se note rápido.
BLOCK_MS = 5_000
BATCH = 16
# Cuánto puede llevar un mensaje leído y sin ackear antes de que otro lo reclame.
MIN_IDLE_MS = 60_000

# Las escalas de las columnas de `fills` en `db/models.py`.
_SHARES = Decimal("0.000001")  # Numeric(20, 6)
_PRICE = Decimal("0.000001")  # Numeric(12, 6)
_MONEY = Decimal("0.000001")  # Numeric(20, 6)
_BPS = Decimal("0.0001")  # Numeric(10, 4)
_ZERO = Decimal("0")


class FillOutcome(StrEnum):
    """Qué pasó con un mensaje. Los cuatro son terminales: volver no lo mejora."""

    WRITTEN = "written"
    # exec re-emitió un fill que ya teníamos, o el índice único lo rechazó.
    DUPLICATE = "duplicate"
    # Llegó un fill de un intent que brain no pidió, o que no puede reconstruir.
    ORPHAN = "orphan"
    # Un fill `live`. La Fase 3 no sabe contabilizarlo.
    NOT_SHADOW = "not_shadow"


@dataclass
class FillConsumerStats:
    read: int = 0
    written: int = 0
    duplicates: int = 0
    orphans: int = 0
    invalid: int = 0
    reclaimed: int = 0
    not_shadow: int = 0


def _parse_ts(raw: str) -> datetime:
    """El `ts` de exec: cuándo se cotizó. Es el instante que la medición quiere."""
    ts = datetime.fromisoformat(raw)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Aplicar un fill. Sin Redis: solo la sesión y el mensaje.
# ---------------------------------------------------------------------------


async def apply_fill(
    session: AsyncSession, msg: FillMessage, now: datetime | None = None
) -> FillOutcome:
    """Escribe el fill y resuelve su intent, en una transacción.

    Devuelve qué pasó. No levanta por un duplicado ni por un huérfano: los dos son
    estados normales de un bus que entrega al menos una vez. Sí deja subir un fallo
    de base de datos, para que el mensaje no se ackee y vuelva.
    """
    now = now or datetime.now(UTC)

    if msg.mode != SHADOW_MODE:
        # La Fase 3 no sabe contabilizar un fill real: no mueve posiciones, y una
        # orden firmada sí las mueve. Escribirla con `action='SHADOW'` la dejaría
        # fuera de toda suma y el dinero desaparecería del libro mayor.
        # La Fase 4 implementa este camino. Hasta entonces, el mensaje se aparta.
        log.error("fills.modo_no_soportado", intent_id=msg.intent_id, mode=msg.mode)
        return FillOutcome.NOT_SHADOW

    intent = await session.get(Intent, msg.intent_id)
    if intent is None:
        log.error("fills.intent_desconocido", intent_id=msg.intent_id)
        return FillOutcome.ORPHAN

    if intent.status is not None:
        # Ya resuelto. exec re-emite el fill que guardó cuando el intent se repite;
        # es su forma de responder sin cotizar dos veces contra libros distintos.
        log.info("fills.duplicado", intent_id=msg.intent_id, status=intent.status)
        return FillOutcome.DUPLICATE

    if intent.mode != msg.mode:  # pragma: no cover — exec copia el mode del intent
        log.error(
            "fills.modo_no_coincide",
            intent_id=msg.intent_id,
            intent_mode=intent.mode,
            fill_mode=msg.mode,
        )
        return FillOutcome.ORPHAN

    fill = Fill(
        ts=_parse_ts(msg.ts),
        signal_id=intent.signal_id,
        market_id=intent.market_id,
        # El idioma de brain: qué posición habría abierto esto.
        side=intent.side,
        action=SHADOW_ACTION,
        shares=msg.filled_shares.quantize(_SHARES),
        # `None` cuando el libro no tenía dos lados. No se inventa un precio.
        mid_at_fill=None if msg.mid_price is None else msg.mid_price.quantize(_PRICE),
        fill_price=msg.avg_price.quantize(_PRICE),
        slippage_bps=(
            None
            if msg.realized_slippage_bps is None
            else Decimal(msg.realized_slippage_bps).quantize(_BPS)
        ),
        notional_usd=msg.notional_usd.quantize(_MONEY),
        fees_usd=msg.fees_usd.quantize(_MONEY),
        # Una medición no realiza nada. Cero, siempre.
        realized_pnl_usd=_ZERO,
        mode=msg.mode,
        intent_id=msg.intent_id,
        status=msg.status,
        order_id=msg.order_id or None,
        tx_hash=msg.tx_hash or None,
    )
    session.add(fill)

    intent.status = msg.status
    intent.resolved_at = now
    intent.error = msg.error or None

    try:
        await session.commit()
    except IntegrityError:
        # El índice único de `fills.intent_id`. Otro consumidor —o este mismo antes
        # de morir— ya lo escribió. La idempotencia la impone Postgres.
        await session.rollback()
        log.info("fills.duplicado_por_indice", intent_id=msg.intent_id)
        return FillOutcome.DUPLICATE

    log.info(
        "fills.written",
        intent_id=msg.intent_id,
        status=msg.status,
        market_id=intent.market_id,
        expected_slippage_bps=(
            None if intent.expected_slippage_bps is None else float(intent.expected_slippage_bps)
        ),
        realized_slippage_bps=msg.realized_slippage_bps,
        notional_usd=float(msg.notional_usd),
    )
    return FillOutcome.WRITTEN


# ---------------------------------------------------------------------------
# El loop
# ---------------------------------------------------------------------------


class FillConsumer:
    """Lee `nocti:fills` con el grupo `brain` y aplica cada mensaje.

    Un solo cliente de Redis, y no dos como en exec: `redis-py` bloquea sobre una
    conexión del pool, no sobre la única del cliente, así que el `XACK` que sigue
    al `XREADGROUP` no se queda esperando a nadie.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        consumer_name: str = "brain-1",
        block_ms: int = BLOCK_MS,
        batch: int = BATCH,
        min_idle_ms: int = MIN_IDLE_MS,
    ) -> None:
        self._sm = sessionmaker or get_sessionmaker()
        self.consumer_name = consumer_name
        self.block_ms = block_ms
        self.batch = batch
        self.min_idle_ms = min_idle_ms
        self.stats = FillConsumerStats()

    async def ensure_group(self) -> None:
        """Crea el grupo desde `0`, no desde `$`.

        Con `$` se ignoraría todo lo que exec publicó antes de que brain arrancara
        por primera vez. Un fill perdido es una medición perdida, y en la Fase 4
        sería una orden real sin registro.
        """
        redis = get_redis()
        try:
            await redis.xgroup_create(FILLS_STREAM, BRAIN_GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _dead_letter(self, fields: dict[str, str], error: str) -> None:
        """Un mensaje que no se puede aplicar no se ejecuta ni se adivina. Se aparta."""
        redis = get_redis()
        await redis.xadd(
            DEAD_LETTER_STREAM,
            {**fields, "_error": error},
            maxlen=DEAD_LETTER_MAXLEN,
            approximate=True,
        )

    async def _consume(self, entry_id: str, raw: dict[str, str]) -> bool:
        """Procesa una entrada. `True` si hay que ackearla."""
        self.stats.read += 1
        fields = fields_from_entry(raw)

        try:
            msg = parse_fill(fields)
        except ContractError as exc:
            self.stats.invalid += 1
            log.warning("fills.descartado", error=str(exc))
            await self._dead_letter(fields, str(exc))
            return True

        async with self._sm() as session:
            outcome = await apply_fill(session, msg)

        if outcome is FillOutcome.WRITTEN:
            self.stats.written += 1
        elif outcome is FillOutcome.DUPLICATE:
            self.stats.duplicates += 1
        elif outcome is FillOutcome.ORPHAN:
            self.stats.orphans += 1
            await self._dead_letter(fields, "sin fila en `intents`, o el mode no coincide")
        elif outcome is FillOutcome.NOT_SHADOW:
            self.stats.not_shadow += 1
            await self._dead_letter(fields, f"mode `{msg.mode}` no soportado en la Fase 3")

        # Los cuatro casos son terminales: el mensaje no mejora si vuelve. Solo un
        # fallo de base de datos deja el `XACK` sin hacer, y ese sube como excepción.
        return True

    async def _reclaim(self) -> None:
        """Recoge lo que otro consumidor leyó y nunca ackeó, porque murió."""
        redis = get_redis()
        _, messages, _ = await redis.xautoclaim(
            FILLS_STREAM,
            BRAIN_GROUP,
            self.consumer_name,
            min_idle_time=self.min_idle_ms,
            start_id="0-0",
            count=self.batch,
        )
        for entry_id, raw in messages:
            self.stats.reclaimed += 1
            if await self._consume(entry_id, raw):
                await redis.xack(FILLS_STREAM, BRAIN_GROUP, entry_id)

    async def _read_new(self) -> None:
        redis = get_redis()
        res = await redis.xreadgroup(
            BRAIN_GROUP,
            self.consumer_name,
            {FILLS_STREAM: ">"},
            count=self.batch,
            block=self.block_ms,
        )
        for _stream, entries in res or []:
            for entry_id, raw in entries:
                if await self._consume(entry_id, raw):
                    await redis.xack(FILLS_STREAM, BRAIN_GROUP, entry_id)

    async def tick(self) -> None:
        await self._reclaim()
        await self._read_new()

    async def run(self, stop: asyncio.Event) -> None:
        await self.ensure_group()
        log.info("fills.consumer_started", consumer=self.consumer_name)
        while not stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:
                # Redis o Postgres caídos. Lo que no se ackeó vuelve por XAUTOCLAIM.
                log.warning("fills.tick_failed", error=repr(exc))
                await asyncio.sleep(1.0)
        log.info("fills.consumer_stopped", **vars(self.stats))
