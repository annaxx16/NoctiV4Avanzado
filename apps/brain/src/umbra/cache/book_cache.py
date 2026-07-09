"""Hot cache del último book por mercado.

Clave: book:{condition_id}
TTL: 60s (configurable). Si pasa más sin update, se considera stale.

Dos productores posibles, distinguidos por el campo `source`:

- `gamma_poll`: el poller de brain, cada 30s contra la API REST de Gamma.
  Solo trae top of book (best_bid/best_ask), sin profundidad.
- `clob_ws`: el publicador de exec, en tiempo real desde el WebSocket oficial.
  Trae además `bids`/`asks` con la profundidad real del libro.

`bids`/`asks` son opcionales a propósito: los books que escribe el poller no los
tienen, y un lector que no los conozca sigue funcionando. Cuando están, dejan de
hacer falta las heurísticas que usan `volume_24hr` como proxy de liquidez.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime

from umbra.cache.redis_client import get_redis

BOOK_TTL_SEC = 60
KEY_PREFIX = "book:"

SOURCE_GAMMA_POLL = "gamma_poll"
SOURCE_CLOB_WS = "clob_ws"

# Precio y tamaño viajan como string: un float de 64 bits no representa 0.62
# exactamente, y aquí eso es dinero. Ver packages/contracts/README.md.
Level = list[str]  # [precio, tamaño]


@dataclass
class CachedBook:
    condition_id: str
    ts: str  # ISO-8601
    best_bid: float | None
    best_ask: float | None
    last_trade_price: float | None
    spread: float | None
    liquidity_num: float | None
    volume_24hr: float | None

    # --- Extensión Fase 1. Ausentes en los books que escribe el poller. ---
    bids: list[Level] | None = None
    asks: list[Level] | None = None
    source: str = SOURCE_GAMMA_POLL

    @property
    def has_depth(self) -> bool:
        """¿Trae profundidad real del libro, o solo top of book?"""
        return bool(self.bids) and bool(self.asks)


def _key(condition_id: str) -> str:
    return f"{KEY_PREFIX}{condition_id}"


async def set_book(book: CachedBook, ttl: int = BOOK_TTL_SEC) -> None:
    redis = get_redis()
    await redis.set(_key(book.condition_id), json.dumps(asdict(book)), ex=ttl)


async def get_book(condition_id: str) -> CachedBook | None:
    redis = get_redis()
    raw = await redis.get(_key(condition_id))
    if raw is None:
        return None
    return decode_book(raw)


def decode_book(raw: str) -> CachedBook:
    """Deserializa tolerando campos desconocidos.

    Durante un despliegue escalonado exec puede correr una versión más nueva del
    contrato que brain. Un campo de más no debe tirar al lector.
    """
    data = json.loads(raw)
    known = {f.name for f in fields(CachedBook)}
    return CachedBook(**{k: v for k, v in data.items() if k in known})


def age_seconds(book: CachedBook, *, now: datetime | None = None) -> float:
    """Antigüedad del snapshot en segundos.

    Un `ts` que no se pueda parsear cuenta como infinitamente viejo: ante la duda,
    el book está rancio. Es la dirección segura del error.
    """
    reference = now or datetime.now(UTC)
    try:
        ts = datetime.fromisoformat(book.ts)
    except ValueError:
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (reference - ts).total_seconds()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
