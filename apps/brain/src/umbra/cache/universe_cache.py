"""El universo de mercados vivos, publicado en Redis para que exec lo lea.

Clave: nocti:universe

brain es el dueño de Postgres y el único que decide qué mercados se vigilan.
exec no habla con Postgres: no tiene credenciales de la base de datos ni le hacen
falta. Solo habla dos idiomas, Redis y Polymarket.

El TTL es deliberado. Si brain muere, el universo caduca, exec se desuscribe de
todo y deja de publicar books. Entonces los books caducan también (TTL 60s) y,
cuando brain vuelva, su poller no encontrará nada fresco y caerá a Gamma por su
cuenta. El sistema degrada solo, sin que nadie tenga que darse cuenta.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime

from umbra.cache.redis_client import get_redis
from umbra.config import settings

UNIVERSE_KEY = "nocti:universe"


@dataclass
class UniverseMarket:
    condition_id: str
    rank: int
    # Los token IDs del CTF, en el orden de `outcomes`. exec se suscribe al
    # WebSocket por token_id, no por condition_id.
    token_ids: list[str]
    # Vienen de Gamma. El WebSocket no los da, así que viajan aquí para que exec
    # pueda componer el book completo sin llamar a Gamma.
    liquidity_num: float | None
    volume_24hr: float | None


@dataclass
class Universe:
    ts: str  # ISO-8601
    markets: list[UniverseMarket]


def _ttl_seconds() -> int:
    """Cuatro rondas de escaneo. Tolera que brain se salte alguna sin dejar ciego a exec."""
    return max(60, settings.universe_scan_interval_sec * 4)


async def publish_universe(markets: list[UniverseMarket]) -> None:
    redis = get_redis()
    payload = Universe(ts=datetime.now(UTC).isoformat(), markets=markets)
    await redis.set(UNIVERSE_KEY, json.dumps(asdict(payload)), ex=_ttl_seconds())


async def get_universe() -> Universe | None:
    redis = get_redis()
    raw = await redis.get(UNIVERSE_KEY)
    if raw is None:
        return None
    return decode_universe(raw)


def decode_universe(raw: str) -> Universe:
    data = json.loads(raw)
    known = {f.name for f in fields(UniverseMarket)}
    return Universe(
        ts=data["ts"],
        markets=[
            UniverseMarket(**{k: v for k, v in m.items() if k in known})
            for m in data.get("markets", [])
        ],
    )
