"""Modelos SQLAlchemy de umbraNocti.

- Market: metadata estable del mercado (1 fila por condition_id).
- BookSnapshot: serie temporal de precios/spread por mercado.
- MarketActive: universo filtrado (vivo en cualquier momento, no historial).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from umbra.db.base import Base


class Market(Base):
    __tablename__ = "markets"

    condition_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    gamma_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(200), index=True)
    question: Mapped[str] = mapped_column(Text)

    clob_token_ids: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    outcomes: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)

    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    snapshots: Mapped[list[BookSnapshot]] = relationship(
        back_populates="market", cascade="all, delete-orphan"
    )


class BookSnapshot(Base):
    __tablename__ = "book_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(
        String(80), ForeignKey("markets.condition_id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    best_bid: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    best_ask: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    last_trade_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    spread: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))

    liquidity_num: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    volume_24hr: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    accepting_orders: Mapped[bool] = mapped_column(Boolean, default=True)

    market: Mapped[Market] = relationship(back_populates="snapshots")

    __table_args__ = (
        Index("ix_book_snapshots_market_ts", "market_id", "ts"),
    )


class MarketActive(Base):
    """Universo activo: una fila por mercado que cumple criterios de scanning.

    No es histórico. Se refresca por el scanner. Si un mercado deja de cumplir
    criterios, se elimina de aquí (pero su row en `markets` y sus snapshots quedan).
    """

    __tablename__ = "markets_active"

    condition_id: Mapped[str] = mapped_column(
        String(80), ForeignKey("markets.condition_id", ondelete="CASCADE"), primary_key=True
    )
    rank: Mapped[int] = mapped_column(BigInteger, index=True)
    liquidity_num: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    volume_24hr: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    market_id: Mapped[str] = mapped_column(
        String(80), ForeignKey("markets.condition_id", ondelete="CASCADE"), index=True
    )
    edge_name: Mapped[str] = mapped_column(String(40))
    side: Mapped[str] = mapped_column(String(16))  # BUY_YES | BUY_NO

    market_price: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    fair_price: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    edge_value: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    strength: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))

    size_shares: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    notional_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))

    accepted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(8))

    __table_args__ = (Index("ix_signals_market_ts", "market_id", "ts"),)


class SignalAudit(Base):
    """Auditoria normalizada de cada senal evaluada por el orchestrator."""

    __tablename__ = "signal_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="SET NULL"), index=True, nullable=True
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    market_id: Mapped[str] = mapped_column(
        String(80), ForeignKey("markets.condition_id", ondelete="CASCADE"), index=True
    )
    market_name: Mapped[str | None] = mapped_column(Text)
    edge_name: Mapped[str] = mapped_column(String(40), index=True)
    score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    direction: Mapped[str] = mapped_column(String(16))

    accepted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    rejected: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    rejected_reason: Mapped[str | None] = mapped_column(Text)

    risk_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    liquidity_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    exposure_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    composite_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    execution_blocked: Mapped[bool] = mapped_column(Boolean, default=False)

    metadata_json: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_signal_audit_market_ts", "market_id", "timestamp"),
        Index("ix_signal_audit_edge_ts", "edge_name", "timestamp"),
    )


class PaperFill(Base):
    """Fill simulado contra el book real (paper trading).

    `action`:
      - 'OPEN'  → shares >  0, abre o aumenta la posición.
      - 'CLOSE' → shares <  0, reduce o cierra. `realized_pnl_usd` distinto de 0.
    """

    __tablename__ = "fills_paper"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    signal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="CASCADE"), index=True, nullable=True
    )
    market_id: Mapped[str] = mapped_column(
        String(80), ForeignKey("markets.condition_id", ondelete="CASCADE"), index=True
    )
    side: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(8), default="OPEN")

    shares: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    mid_at_fill: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    fill_price: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    slippage_bps: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    notional_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    fees_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    realized_pnl_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), default=Decimal("0")
    )

    mode: Mapped[str] = mapped_column(String(8))


class TradeOutcome(Base):
    """Resultado normalizado de cada cierre de paper/live simulado."""

    __tablename__ = "trade_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    close_fill_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fills_paper.id", ondelete="CASCADE"), unique=True, index=True
    )
    entry_signal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("signals.id", ondelete="SET NULL"), index=True, nullable=True
    )
    market_id: Mapped[str] = mapped_column(
        String(80), ForeignKey("markets.condition_id", ondelete="CASCADE"), index=True
    )
    side: Mapped[str] = mapped_column(String(16))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    exit_price: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    holding_time_hours: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    return_pct: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    profit_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    loss_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    realized_pnl_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    winning_trade: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    losing_trade: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    edge_source: Mapped[str | None] = mapped_column(String(40), index=True)
    exit_reason: Mapped[str | None] = mapped_column(String(80))
    market_conditions: Mapped[dict | None] = mapped_column(JSON)
    mode: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_trade_outcomes_edge_closed", "edge_source", "closed_at"),
        Index("ix_trade_outcomes_market_closed", "market_id", "closed_at"),
    )


class EdgePerformance(Base):
    """Metricas agregadas por edge para aprendizaje estadistico."""

    __tablename__ = "edge_performance"

    edge_name: Mapped[str] = mapped_column(String(40), primary_key=True)
    signals_generated: Mapped[int] = mapped_column(BigInteger, default=0)
    signals_accepted: Mapped[int] = mapped_column(BigInteger, default=0)
    trades_executed: Mapped[int] = mapped_column(BigInteger, default=0)
    wins: Mapped[int] = mapped_column(BigInteger, default=0)
    losses: Mapped[int] = mapped_column(BigInteger, default=0)
    avg_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    profit_factor: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    sharpe: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    expectancy: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    max_drawdown: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    rolling_7d: Mapped[dict | None] = mapped_column(JSON)
    rolling_30d: Mapped[dict | None] = mapped_column(JSON)
    rolling_100_trades: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EdgeWeight(Base):
    """Peso dinamico calculado para un edge, aun no aplicado a ejecucion."""

    __tablename__ = "edge_weights"

    edge_name: Mapped[str] = mapped_column(
        String(40), ForeignKey("edge_performance.edge_name", ondelete="CASCADE"), primary_key=True
    )
    raw_score: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    weight: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=Decimal("0.05"))
    profit_factor: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    expectancy: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    sharpe: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    stability_score: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    rolling_30d_score: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    rolling_100_trades_score: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LearningSnapshot(Base):
    """Snapshot historico del learning loop estadistico."""

    __tablename__ = "learning_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="ok", index=True)
    edges_evaluated: Mapped[int] = mapped_column(BigInteger, default=0)
    weights_updated: Mapped[int] = mapped_column(BigInteger, default=0)
    report_json: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class PaperPosition(Base):
    """Posición acumulada por (market, side) en paper trading."""

    __tablename__ = "portfolio_state"

    market_id: Mapped[str] = mapped_column(
        String(80),
        ForeignKey("markets.condition_id", ondelete="CASCADE"),
        primary_key=True,
    )
    side: Mapped[str] = mapped_column(String(16), primary_key=True)

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    shares: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    avg_entry_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0")
    )
    total_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), default=Decimal("0")
    )
    total_fees_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), default=Decimal("0")
    )
    realized_pnl_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), default=Decimal("0")
    )
    peak_unrealized_pnl_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), default=Decimal("0")
    )
    n_fills: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(12), default="open")  # open/closed


class Outcome(Base):
    """Resolución de un mercado. 1 fila por market.

    Permite mark-to-market real para posiciones en mercados resueltos:
    valor = shares * (1 si lado coincide con outcome, 0 si no).
    """

    __tablename__ = "outcomes"

    market_id: Mapped[str] = mapped_column(
        String(80),
        ForeignKey("markets.condition_id", ondelete="CASCADE"),
        primary_key=True,
    )
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    yes_outcome: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(40), default="gamma_api")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EquitySnapshot(Base):
    """Punto de la curva de equity REAL (no cost-basis).

    Persistido por el equity_snapshot_loop cada N segundos.
    """

    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    cash_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    positions_value_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    equity_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    unrealized_pnl_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    realized_pnl_usd_total: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    gross_exposure_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    peak_equity_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    n_open_positions: Mapped[int] = mapped_column(Integer)


class OhlcBar(Base):
    """Vela OHLC agregada a partir de book_snapshots, por (market, interval, bucket).

    `volume_proxy` es un proxy débil (avg volume_24hr en el bucket) — Polymarket
    no devuelve volumen por vela. Se usa solo para colorear/sizing visual.
    """

    __tablename__ = "ohlc_bars"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(
        String(80), ForeignKey("markets.condition_id", ondelete="CASCADE")
    )
    interval: Mapped[str] = mapped_column(String(8))  # '1m'|'5m'|'15m'|'1h'
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    open_price: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    high_price: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    low_price: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    close_price: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    volume_proxy: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    n_snapshots: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint(
            "market_id", "interval", "bucket_start", name="uq_ohlc_market_interval_bucket"
        ),
        Index("ix_ohlc_market_interval_ts", "market_id", "interval", "bucket_start"),
    )
