"""P0 — exits, outcomes, equity snapshots, ohlc bars

Revision ID: 1a2b3c4d5e6f
Revises: ea96572a4bda
Create Date: 2026-05-20 12:00:00.000000

Cambios:
- fills_paper.action ('OPEN'|'CLOSE'), fills_paper.realized_pnl_usd
- portfolio_state.realized_pnl_usd, .closed_at, .peak_unrealized_pnl_usd
- new outcomes (resolución de mercados, para mark-to-real)
- new equity_snapshots (curva de equity REAL, no cost-basis)
- new ohlc_bars (candlesticks para P1 — análisis técnico)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "1a2b3c4d5e6f"
down_revision: Union[str, None] = "ea96572a4bda"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "fills_paper",
        sa.Column("action", sa.String(length=8), nullable=False, server_default="OPEN"),
    )
    op.add_column(
        "fills_paper",
        sa.Column(
            "realized_pnl_usd",
            sa.Numeric(precision=20, scale=6),
            nullable=False,
            server_default="0",
        ),
    )

    op.add_column(
        "portfolio_state",
        sa.Column(
            "realized_pnl_usd",
            sa.Numeric(precision=20, scale=6),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "portfolio_state",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "portfolio_state",
        sa.Column(
            "peak_unrealized_pnl_usd",
            sa.Numeric(precision=20, scale=6),
            nullable=False,
            server_default="0",
        ),
    )

    op.create_table(
        "outcomes",
        sa.Column("market_id", sa.String(length=80), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("yes_outcome", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="gamma_api"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["market_id"], ["markets.condition_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("market_id"),
    )

    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("cash_usd", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("positions_value_usd", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("equity_usd", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("unrealized_pnl_usd", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column(
            "realized_pnl_usd_total", sa.Numeric(precision=20, scale=6), nullable=False
        ),
        sa.Column("gross_exposure_usd", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("peak_equity_usd", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("drawdown_pct", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("n_open_positions", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_equity_snapshots_ts"), "equity_snapshots", ["ts"], unique=False
    )

    op.create_table(
        "ohlc_bars",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market_id", sa.String(length=80), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_price", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("high_price", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("low_price", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("close_price", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("volume_proxy", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("n_snapshots", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.condition_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "market_id", "interval", "bucket_start", name="uq_ohlc_market_interval_bucket"
        ),
    )
    op.create_index(
        "ix_ohlc_market_interval_ts",
        "ohlc_bars",
        ["market_id", "interval", "bucket_start"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ohlc_market_interval_ts", table_name="ohlc_bars")
    op.drop_table("ohlc_bars")

    op.drop_index(op.f("ix_equity_snapshots_ts"), table_name="equity_snapshots")
    op.drop_table("equity_snapshots")

    op.drop_table("outcomes")

    op.drop_column("portfolio_state", "peak_unrealized_pnl_usd")
    op.drop_column("portfolio_state", "closed_at")
    op.drop_column("portfolio_state", "realized_pnl_usd")

    op.drop_column("fills_paper", "realized_pnl_usd")
    op.drop_column("fills_paper", "action")
