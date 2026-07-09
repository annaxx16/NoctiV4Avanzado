"""add signal audit table

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
Create Date: 2026-06-12 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3c4d5e6f7a8b"
down_revision: Union[str, None] = "2b3c4d5e6f7a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "signal_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("signal_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("market_id", sa.String(length=80), nullable=False),
        sa.Column("market_name", sa.Text(), nullable=True),
        sa.Column("edge_name", sa.String(length=40), nullable=False),
        sa.Column("score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("rejected", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column(
            "risk_blocked", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "liquidity_blocked", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "exposure_blocked", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "composite_blocked", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "execution_blocked", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["market_id"], ["markets.condition_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_signal_audit_signal_id"), "signal_audit", ["signal_id"])
    op.create_index(op.f("ix_signal_audit_timestamp"), "signal_audit", ["timestamp"])
    op.create_index(op.f("ix_signal_audit_market_id"), "signal_audit", ["market_id"])
    op.create_index(op.f("ix_signal_audit_edge_name"), "signal_audit", ["edge_name"])
    op.create_index(op.f("ix_signal_audit_accepted"), "signal_audit", ["accepted"])
    op.create_index(op.f("ix_signal_audit_rejected"), "signal_audit", ["rejected"])
    op.create_index(
        "ix_signal_audit_market_ts", "signal_audit", ["market_id", "timestamp"]
    )
    op.create_index("ix_signal_audit_edge_ts", "signal_audit", ["edge_name", "timestamp"])


def downgrade() -> None:
    op.drop_index("ix_signal_audit_edge_ts", table_name="signal_audit")
    op.drop_index("ix_signal_audit_market_ts", table_name="signal_audit")
    op.drop_index(op.f("ix_signal_audit_rejected"), table_name="signal_audit")
    op.drop_index(op.f("ix_signal_audit_accepted"), table_name="signal_audit")
    op.drop_index(op.f("ix_signal_audit_edge_name"), table_name="signal_audit")
    op.drop_index(op.f("ix_signal_audit_market_id"), table_name="signal_audit")
    op.drop_index(op.f("ix_signal_audit_timestamp"), table_name="signal_audit")
    op.drop_index(op.f("ix_signal_audit_signal_id"), table_name="signal_audit")
    op.drop_table("signal_audit")
