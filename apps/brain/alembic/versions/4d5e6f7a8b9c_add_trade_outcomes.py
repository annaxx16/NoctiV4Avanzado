"""add trade outcomes table

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-06-12 11:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4d5e6f7a8b9c"
down_revision: Union[str, None] = "3c4d5e6f7a8b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_outcomes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("close_fill_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_signal_id", sa.BigInteger(), nullable=True),
        sa.Column("market_id", sa.String(length=80), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("exit_price", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("holding_time_hours", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("return_pct", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("profit_usd", sa.Numeric(precision=20, scale=6), nullable=False, server_default="0"),
        sa.Column("loss_usd", sa.Numeric(precision=20, scale=6), nullable=False, server_default="0"),
        sa.Column(
            "realized_pnl_usd",
            sa.Numeric(precision=20, scale=6),
            nullable=False,
            server_default="0",
        ),
        sa.Column("winning_trade", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("losing_trade", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("edge_source", sa.String(length=40), nullable=True),
        sa.Column("exit_reason", sa.String(length=80), nullable=True),
        sa.Column("market_conditions", sa.JSON(), nullable=True),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["close_fill_id"], ["fills_paper.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entry_signal_id"], ["signals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["market_id"], ["markets.condition_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("close_fill_id"),
    )
    op.create_index(op.f("ix_trade_outcomes_close_fill_id"), "trade_outcomes", ["close_fill_id"])
    op.create_index(op.f("ix_trade_outcomes_entry_signal_id"), "trade_outcomes", ["entry_signal_id"])
    op.create_index(op.f("ix_trade_outcomes_market_id"), "trade_outcomes", ["market_id"])
    op.create_index(op.f("ix_trade_outcomes_closed_at"), "trade_outcomes", ["closed_at"])
    op.create_index(op.f("ix_trade_outcomes_winning_trade"), "trade_outcomes", ["winning_trade"])
    op.create_index(op.f("ix_trade_outcomes_losing_trade"), "trade_outcomes", ["losing_trade"])
    op.create_index(op.f("ix_trade_outcomes_edge_source"), "trade_outcomes", ["edge_source"])
    op.create_index(
        "ix_trade_outcomes_edge_closed", "trade_outcomes", ["edge_source", "closed_at"]
    )
    op.create_index(
        "ix_trade_outcomes_market_closed", "trade_outcomes", ["market_id", "closed_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_trade_outcomes_market_closed", table_name="trade_outcomes")
    op.drop_index("ix_trade_outcomes_edge_closed", table_name="trade_outcomes")
    op.drop_index(op.f("ix_trade_outcomes_edge_source"), table_name="trade_outcomes")
    op.drop_index(op.f("ix_trade_outcomes_losing_trade"), table_name="trade_outcomes")
    op.drop_index(op.f("ix_trade_outcomes_winning_trade"), table_name="trade_outcomes")
    op.drop_index(op.f("ix_trade_outcomes_closed_at"), table_name="trade_outcomes")
    op.drop_index(op.f("ix_trade_outcomes_market_id"), table_name="trade_outcomes")
    op.drop_index(op.f("ix_trade_outcomes_entry_signal_id"), table_name="trade_outcomes")
    op.drop_index(op.f("ix_trade_outcomes_close_fill_id"), table_name="trade_outcomes")
    op.drop_table("trade_outcomes")
