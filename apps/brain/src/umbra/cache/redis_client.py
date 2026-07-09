"""Cliente Redis async con cache por event loop.

En producción solo hay un event loop, así que se comporta como singleton.
En tests, pytest-asyncio crea un loop nuevo por test — un singleton global se
queda atado al primer loop y rompe los demás con `RuntimeError: Event loop is
closed`. El cache por id(loop) evita ese problema.
"""

from __future__ import annotations

import asyncio

from redis.asyncio import Redis, from_url

from umbra.config import settings

_clients: dict[int, Redis] = {}


def _loop_id() -> int:
    try:
        return id(asyncio.get_event_loop())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return id(loop)


def get_redis() -> Redis:
    lid = _loop_id()
    client = _clients.get(lid)
    if client is None:
        client = from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=15,
            socket_connect_timeout=10,
        )
        _clients[lid] = client
    return client


async def ping() -> bool:
    """Verifica que Redis responda sin asumir el estado del kill switch."""
    redis = get_redis()
    try:
        return bool(await redis.ping())
    except Exception:
        return False


async def dispose() -> None:
    """Cierra el cliente del loop ACTUAL."""
    lid = _loop_id()
    client = _clients.pop(lid, None)
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass


async def dispose_all() -> None:
    """Best-effort: limpia el cache. No cierra conexiones de otros loops."""
    _clients.clear()
