"""Configuración global de los tests.

Tres cosas, y la primera es la que importa:

1. **Los tests no pueden tocar la base de datos de producción.** No es una
   hipótesis: pasó. Correr `pytest` sin `.env` hacía que `settings.database_url`
   cayera al valor por defecto, que resulta ser la base real. Los tests
   insertaron 31 `book_snapshots`, 12 `markets` y 2 `learning_snapshots` dentro
   de la serie histórica que alimenta la validación del edge. Se limpiaron a
   mano. Nunca más: aquí se apunta a otro sitio antes de que nadie abra el engine.

2. psycopg async no funciona con el ProactorEventLoop de Windows, y pytest-asyncio
   usaría el policy por defecto si no lo cambiamos antes.

3. Un fixture autouse que limpia los mercados sintéticos (`0xtest_`) de runs
   anteriores, para que un test que falle no contamine al siguiente.
"""

import asyncio
import os
import sys

import pytest_asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# --------------------------------------------------------------------------
# Aislamiento de la base de datos. Ocurre en tiempo de import de conftest, antes
# de que ningún test importe `umbra.db.session` y cachee el engine.
# --------------------------------------------------------------------------

_SENTINEL = (
    "postgresql+psycopg://umbra:umbra_dev@localhost:5432/"
    "umbra_NO_TEST_DB__define_UMBRA_TEST_DATABASE_URL"
)


def _resolve_test_database_url() -> str:
    """La URL contra la que los tests tienen permiso de escribir.

    Sin `UMBRA_TEST_DATABASE_URL` devolvemos una URL que no existe. Los tests de
    lógica pura (sizer, overreaction, slippage, features, leakage) siguen pasando
    porque no tocan la base; los de integración fallan con un error que se explica
    solo, en vez de escribir en producción y no decir nada.

    Fallar ruidosamente es la dirección segura del error. Saltarse el test en
    silencio es lo que permitió que esto pasara.
    """
    url = os.environ.get("UMBRA_TEST_DATABASE_URL")
    if not url:
        return _SENTINEL

    db_name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in db_name.lower():
        raise RuntimeError(
            f"UMBRA_TEST_DATABASE_URL apunta a la base '{db_name}', que no parece "
            "de tests. Los tests escriben y borran filas. Usa una base cuyo nombre "
            "contenga 'test' (p.ej. el Postgres del perfil `db` de compose, en el "
            "puerto 5434)."
        )
    return url


# Importar `settings` aquí, y no arriba, deja claro que el orden importa: se pisa
# la URL antes de que `get_engine()` la lea por primera vez.
from umbra.config import settings  # noqa: E402

settings.database_url = _resolve_test_database_url()


@pytest_asyncio.fixture(autouse=True)
async def _wipe_synthetic_test_state():
    """Antes de cada test, borra cualquier residuo `0xtest_%` de runs anteriores.

    Después del test, repite la limpieza para no contaminar el siguiente.
    """
    from sqlalchemy import text

    from umbra.cache.redis_client import dispose as redis_dispose
    from umbra.db.session import get_sessionmaker

    sm = get_sessionmaker()

    async def _purge() -> None:
        async with sm() as session:
            await session.execute(
                text(
                    "DELETE FROM fills_paper WHERE market_id LIKE '0xtest_%' OR "
                    "signal_id IN (SELECT id FROM signals WHERE market_id LIKE '0xtest_%')"
                )
            )
            await session.execute(
                text("DELETE FROM portfolio_state WHERE market_id LIKE '0xtest_%'")
            )
            await session.execute(
                text("DELETE FROM signals WHERE market_id LIKE '0xtest_%'")
            )
            await session.execute(
                text("DELETE FROM outcomes WHERE market_id LIKE '0xtest_%'")
            )
            await session.execute(
                text("DELETE FROM book_snapshots WHERE market_id LIKE '0xtest_%'")
            )
            await session.execute(
                text("DELETE FROM markets WHERE condition_id LIKE '0xtest_%'")
            )
            await session.commit()

    # Tolerante a entornos sin Postgres/Redis: los tests de lógica pura no tocan
    # la DB y deben poder correr offline. Si la limpieza falla por falta de infra,
    # la saltamos; los de integración fallarán por su cuenta con un error claro.
    db_ok = True
    try:
        await _purge()
    except Exception:
        db_ok = False
    try:
        yield
    finally:
        if db_ok:
            try:
                await _purge()
            except Exception:
                pass
        try:
            # Cerrar el cliente Redis atado a este loop antes de que pytest lo destruya
            await redis_dispose()
        except Exception:
            pass
