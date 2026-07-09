"""add learning snapshots table

Revision ID: 7a8b9c0d1e2f
Revises: 6f7a8b9c0d1e
Create Date: 2026-06-12 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7a8b9c0d1e2f"
down_revision: Union[str, None] = "6f7a8b9c0d1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "learning_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ok"),
        sa.Column("edges_evaluated", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("weights_updated", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("report_json", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_learning_snapshots_ts"), "learning_snapshots", ["ts"])
    op.create_index(op.f("ix_learning_snapshots_status"), "learning_snapshots", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_learning_snapshots_status"), table_name="learning_snapshots")
    op.drop_index(op.f("ix_learning_snapshots_ts"), table_name="learning_snapshots")
    op.drop_table("learning_snapshots")
