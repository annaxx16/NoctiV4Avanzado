"""add edge performance table

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
Create Date: 2026-06-12 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5e6f7a8b9c0d"
down_revision: Union[str, None] = "4d5e6f7a8b9c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "edge_performance",
        sa.Column("edge_name", sa.String(length=40), nullable=False),
        sa.Column("signals_generated", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("signals_accepted", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("trades_executed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("wins", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("losses", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("avg_return", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("profit_factor", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("sharpe", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("expectancy", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("max_drawdown", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("rolling_7d", sa.JSON(), nullable=True),
        sa.Column("rolling_30d", sa.JSON(), nullable=True),
        sa.Column("rolling_100_trades", sa.JSON(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("edge_name"),
    )


def downgrade() -> None:
    op.drop_table("edge_performance")
