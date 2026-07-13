"""Fase 3: fills_paper -> fills, y la tabla intents

`fills_paper` se llamaba así porque solo había fills de paper. Desde la Fase 3
entran también los que `exec` cotiza contra el libro real (`mode='shadow'`), y
algún día los reales. El nombre mentía.

Tres cosas más:

1. `fills` gana lo que viene del bus: `intent_id` (único — la idempotencia de
   `nocti:fills` la impone Postgres, no el consumidor), `status`, `order_id`,
   `tx_hash`.

2. `mid_at_fill` y `slippage_bps` pasan a ser nulables. Un intent rechazado o
   expirado produce una fila terminal sin libro contra el que medirse; escribir
   un cero en el precio medio sería inventárselo. Es un ensanchamiento: ninguna
   fila existente se toca.

3. Nueva tabla `intents`: el registro de lo que brain pidió, independiente de lo
   que pasó. Sin ella no se pueden auditar los rechazos de `exec` — un intent que
   muere en el bus no deja rastro en `fills`, y el silencio se confunde con «no
   hubo señal».

La tabla `risk_state` que este plan preveía no se crea: la Fase 2 resolvió que el
estado de riesgo de `exec` vive en Redis (`nocti:exec:risk_state`), porque `exec`
no habla con Postgres.

Revision ID: 8b9c0d1e2f3a
Revises: 7a8b9c0d1e2f
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8b9c0d1e2f3a"
down_revision: Union[str, None] = "7a8b9c0d1e2f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. El rename, con sus índices -------------------------------------
    # `rename_table` no arrastra los nombres de los índices. Dejarlos como
    # `ix_fills_paper_*` colgando de una tabla llamada `fills` es la clase de
    # deuda que se descubre a las tres de la mañana.
    op.rename_table("fills_paper", "fills")
    op.execute("ALTER INDEX ix_fills_paper_ts RENAME TO ix_fills_ts")
    op.execute("ALTER INDEX ix_fills_paper_signal_id RENAME TO ix_fills_signal_id")
    op.execute("ALTER INDEX ix_fills_paper_market_id RENAME TO ix_fills_market_id")
    op.execute("ALTER TABLE fills RENAME CONSTRAINT fills_paper_pkey TO fills_pkey")

    # --- 2. Lo que viene del bus -------------------------------------------
    op.add_column("fills", sa.Column("intent_id", postgresql.UUID(as_uuid=False), nullable=True))
    op.create_index(op.f("ix_fills_intent_id"), "fills", ["intent_id"], unique=True)

    # `server_default` para las filas que ya existen: todas se llenaron, ninguna
    # nació de un intent. Se retira acto seguido para que la aplicación tenga que
    # decir el status explícitamente en cada fill nueva.
    op.add_column(
        "fills",
        sa.Column("status", sa.String(length=12), nullable=False, server_default="FILLED"),
    )
    op.alter_column("fills", "status", server_default=None)

    op.add_column("fills", sa.Column("order_id", sa.String(length=80), nullable=True))
    op.add_column("fills", sa.Column("tx_hash", sa.String(length=80), nullable=True))

    # --- 3. Un fill terminal puede no tener libro --------------------------
    op.alter_column("fills", "mid_at_fill", existing_type=sa.Numeric(12, 6), nullable=True)
    op.alter_column("fills", "slippage_bps", existing_type=sa.Numeric(10, 4), nullable=True)

    # --- 4. Lo que brain pidió ---------------------------------------------
    op.create_table(
        "intents",
        sa.Column("intent_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("signal_id", sa.BigInteger(), nullable=True),
        sa.Column("market_id", sa.String(length=80), nullable=False),
        sa.Column("strategy", sa.String(length=20), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("token_id", sa.String(length=80), nullable=False),
        # El idioma de brain.
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False, server_default="OPEN"),
        # El idioma del bus.
        sa.Column("bus_side", sa.String(length=8), nullable=False),
        sa.Column("size_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("limit_price", sa.Numeric(12, 6), nullable=False),
        sa.Column("tif", sa.String(length=4), nullable=False),
        sa.Column("max_slippage_bps", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_slippage_bps", sa.Numeric(10, 4), nullable=True),
        # El outbox: nulo mientras la fila no se haya escrito en `nocti:intents`.
        # Postgres y Redis no comparten transacción; ver la nota en `db/models.py`.
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        # Nulos mientras el intent sigue en vuelo.
        sa.Column("status", sa.String(length=12), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["market_id"], ["markets.condition_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("intent_id"),
    )
    op.create_index(op.f("ix_intents_ts"), "intents", ["ts"], unique=False)
    op.create_index(op.f("ix_intents_signal_id"), "intents", ["signal_id"], unique=False)
    op.create_index(op.f("ix_intents_market_id"), "intents", ["market_id"], unique=False)
    op.create_index(op.f("ix_intents_strategy"), "intents", ["strategy"], unique=False)
    op.create_index(op.f("ix_intents_mode"), "intents", ["mode"], unique=False)
    op.create_index(op.f("ix_intents_status"), "intents", ["status"], unique=False)
    op.create_index("ix_intents_strategy_ts", "intents", ["strategy", "ts"], unique=False)

    # El backlog del outbox. En régimen normal está vacío: el índice parcial no
    # ocupa nada y el barrido no recorre la tabla entera para encontrar cero filas.
    op.create_index(
        "ix_intents_outbox",
        "intents",
        ["ts"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL AND status IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_intents_outbox", table_name="intents")
    op.drop_index("ix_intents_strategy_ts", table_name="intents")
    op.drop_index(op.f("ix_intents_status"), table_name="intents")
    op.drop_index(op.f("ix_intents_mode"), table_name="intents")
    op.drop_index(op.f("ix_intents_strategy"), table_name="intents")
    op.drop_index(op.f("ix_intents_market_id"), table_name="intents")
    op.drop_index(op.f("ix_intents_signal_id"), table_name="intents")
    op.drop_index(op.f("ix_intents_ts"), table_name="intents")
    op.drop_table("intents")

    # Las filas de medición no tienen sitio en el esquema viejo: `mid_at_fill` y
    # `slippage_bps` vuelven a ser NOT NULL y una fila shadow rechazada los tiene
    # nulos. Se borran. Son mediciones, no contabilidad: nada las echará de menos.
    op.execute("DELETE FROM fills WHERE mode = 'shadow'")
    op.execute("UPDATE fills SET mid_at_fill = 0 WHERE mid_at_fill IS NULL")
    op.execute("UPDATE fills SET slippage_bps = 0 WHERE slippage_bps IS NULL")

    op.alter_column("fills", "slippage_bps", existing_type=sa.Numeric(10, 4), nullable=False)
    op.alter_column("fills", "mid_at_fill", existing_type=sa.Numeric(12, 6), nullable=False)

    op.drop_column("fills", "tx_hash")
    op.drop_column("fills", "order_id")
    op.drop_column("fills", "status")
    op.drop_index(op.f("ix_fills_intent_id"), table_name="fills")
    op.drop_column("fills", "intent_id")

    op.execute("ALTER TABLE fills RENAME CONSTRAINT fills_pkey TO fills_paper_pkey")
    op.execute("ALTER INDEX ix_fills_market_id RENAME TO ix_fills_paper_market_id")
    op.execute("ALTER INDEX ix_fills_signal_id RENAME TO ix_fills_paper_signal_id")
    op.execute("ALTER INDEX ix_fills_ts RENAME TO ix_fills_paper_ts")
    op.rename_table("fills", "fills_paper")
