from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from umbra.analytics.edge_performance import refresh_edge_performance
from umbra.db.models import (
    EdgePerformance,
    Fill,
    Market,
    Signal,
    SignalAudit,
    TradeOutcome,
)
from umbra.db.session import get_sessionmaker

CID = "0xtest_edge_perf"
EDGE = "overreaction_v1"


async def _cleanup(session) -> None:
    await session.execute(delete(EdgePerformance).where(EdgePerformance.edge_name == EDGE))
    await session.execute(delete(TradeOutcome).where(TradeOutcome.market_id == CID))
    await session.execute(delete(SignalAudit).where(SignalAudit.market_id == CID))
    await session.execute(delete(Fill).where(Fill.market_id == CID))
    await session.execute(delete(Signal).where(Signal.market_id == CID))
    await session.execute(delete(Market).where(Market.condition_id == CID))
    await session.commit()


@pytest.mark.asyncio
async def test_refresh_edge_performance_from_audit_and_trade_outcomes():
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session)
        now = datetime.now(UTC)
        session.add(
            Market(
                condition_id=CID,
                gamma_id="gid_edge_perf",
                slug="edge-perf",
                question="Edge performance test",
                clob_token_ids=["t1", "t2"],
                outcomes=["Yes", "No"],
            )
        )
        sig = Signal(
            ts=now - timedelta(minutes=30),
            market_id=CID,
            edge_name=EDGE,
            side="BUY_YES",
            market_price=Decimal("0.40"),
            fair_price=Decimal("0.55"),
            edge_value=Decimal("0.15"),
            strength=Decimal("3.2"),
            size_shares=Decimal("100"),
            notional_usd=Decimal("40"),
            accepted=True,
            reason="ok",
            mode="sim",
        )
        session.add(sig)
        await session.flush()
        session.add(
            SignalAudit(
                signal_id=sig.id,
                timestamp=sig.ts,
                market_id=CID,
                market_name="Edge performance test",
                edge_name=EDGE,
                score=Decimal("0.15"),
                direction="BUY_YES",
                accepted=True,
                rejected=False,
                metadata_json={"test": True},
            )
        )
        close_fill = Fill(
            ts=now,
            signal_id=None,
            market_id=CID,
            side="BUY_YES",
            action="CLOSE",
            shares=Decimal("-100"),
            mid_at_fill=Decimal("0.60"),
            fill_price=Decimal("0.60"),
            slippage_bps=Decimal("20"),
            notional_usd=Decimal("60"),
            fees_usd=Decimal("0"),
            realized_pnl_usd=Decimal("20"),
            mode="sim",
        )
        session.add(close_fill)
        await session.flush()
        session.add(
            TradeOutcome(
                close_fill_id=close_fill.id,
                entry_signal_id=sig.id,
                market_id=CID,
                side="BUY_YES",
                opened_at=now - timedelta(minutes=30),
                closed_at=now,
                entry_price=Decimal("0.40"),
                exit_price=Decimal("0.60"),
                holding_time_hours=Decimal("0.5"),
                return_pct=Decimal("0.5"),
                profit_usd=Decimal("20"),
                loss_usd=Decimal("0"),
                realized_pnl_usd=Decimal("20"),
                winning_trade=True,
                losing_trade=False,
                edge_source=EDGE,
                exit_reason="test",
                market_conditions={"test": True},
                mode="sim",
            )
        )
        await session.flush()
        await refresh_edge_performance(session, EDGE)
        await session.commit()

        perf = (
            await session.execute(
                select(EdgePerformance).where(EdgePerformance.edge_name == EDGE)
            )
        ).scalar_one()
        assert perf.signals_generated == 1
        assert perf.signals_accepted == 1
        assert perf.trades_executed == 1
        assert perf.wins == 1
        assert perf.losses == 0
        assert float(perf.expectancy) == pytest.approx(20.0)
        assert float(perf.avg_return) == pytest.approx(0.5)
        assert perf.rolling_100_trades["trades"] == 1

        await _cleanup(session)
