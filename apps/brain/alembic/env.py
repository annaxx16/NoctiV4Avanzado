"""Alembic env.py — usa sync engine basado en settings.database_url.

SQLAlchemy 2.0 + psycopg 3 funciona en sync sin cambios al URL prefix
(`postgresql+psycopg://`). Para migraciones no necesitamos async.

`UMBRA_TEST_DATABASE_URL` manda sobre `settings.database_url`. Sin esto, la receta
que documenta `docker-compose.yml` —exportar `UMBRA_TEST_DATABASE_URL` y correr
`alembic upgrade head`— **migraba la base de producción**: esa variable solo la
leía `tests/conftest.py`, y Alembic no importa conftest. Es el mismo fallo que
llevó a los tests a escribir 31 `book_snapshots` en la serie histórica real, con
otro disfraz.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from umbra.config import settings
from umbra.db.base import Base

# importar TODOS los modelos para que estén en Base.metadata
from umbra.db import models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_target_url = os.environ.get("UMBRA_TEST_DATABASE_URL") or settings.database_url
config.set_main_option("sqlalchemy.url", _target_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
