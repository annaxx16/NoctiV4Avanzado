"""add edge weights table

Revision ID: 6f7a8b9c0d1e
Revises: 5e6f7a8b9c0d
Create Date: 2026-06-12 13:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6f7a8b9c0d1e"
down_revision: Union[str, None] = "5e6f7a8b9c0d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "edge_weights",
        sa.Column("edge_name", sa.String(length=40), nullable=False),
        sa.Column("raw_score", sa.Numeric(precision=20, scale=6), nullable=False, server_default="0"),
        sa.Column("weight", sa.Numeric(precision=10, scale=6), nullable=False, server_default="0.05"),
        sa.Column("profit_factor", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("expectancy", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("sharpe", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("stability_score", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("rolling_30d_score", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("rolling_100_trades_score", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["edge_name"], ["edge_performance.edge_name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("edge_name"),
    )


def downgrade() -> None:
    op.drop_table("edge_weights")
