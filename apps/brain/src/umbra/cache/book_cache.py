"""Hot cache del último book por mercado.

Clave: book:{condition_id}
TTL: 60s (configurable). Si pasa más sin update, se considera stale.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from umbra.cache.redis_client import get_redis

BOOK_TTL_SEC = 60
KEY_PREFIX = "book:"


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
    data = json.loads(raw)
    return CachedBook(**data)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
