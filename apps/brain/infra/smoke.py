"""Smoke test de conectividad: verifica Postgres + Redis del .env."""

import sys

import psycopg
import redis

from umbra.config import settings
from umbra.logging import configure_logging, get_logger


def check_postgres(url: str) -> tuple[bool, str]:
    sqla_prefix = "postgresql+psycopg://"
    raw = url[len(sqla_prefix) :] if url.startswith(sqla_prefix) else url
    raw = "postgresql://" + raw if not raw.startswith("postgres") else raw
    try:
        with psycopg.connect(raw, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                row = cur.fetchone()
                return True, row[0] if row else "no row"
    except Exception as exc:
        return False, repr(exc)


def check_redis(url: str) -> tuple[bool, str]:
    try:
        client = redis.from_url(url, socket_timeout=10)
        pong = client.ping()
        info = client.info("server")
        return bool(pong), f"redis_version={info.get('redis_version', '?')}"
    except Exception as exc:
        return False, repr(exc)


def main() -> int:
    configure_logging(settings.log_level)
    log = get_logger("umbra.smoke")

    pg_ok, pg_msg = check_postgres(settings.database_url)
    log.info("postgres.check", ok=pg_ok, detail=pg_msg)

    redis_ok, redis_msg = check_redis(settings.redis_url)
    log.info("redis.check", ok=redis_ok, detail=redis_msg)

    return 0 if (pg_ok and redis_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
