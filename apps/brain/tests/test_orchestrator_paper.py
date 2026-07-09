"""E2E: orchestrator genera Signal aceptada y se crea PaperFill + PaperPosition."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from umbra.config import settings
from umbra.db.models import (
    BookSnapshot,
    Market,
    PaperFill,
    PaperPosition,
    Signal,
    SignalAudit,
)
from umbra.db.session import get_sessionmaker
from umbra.engine.orchestrator import evaluate_market

TEST_CID = "0xtest_paper_e2e_synthetic"


async def _cleanup(session) -> None:
    await session.execute(delete(SignalAudit).where(SignalAudit.market_id == TEST_CID))
    await session.execute(delete(PaperFill).where(PaperFill.market_id == TEST_CID))
    await session.execute(
        delete(PaperPosition).where(PaperPosition.market_id == TEST_CID)
    )
    await session.execute(delete(Signal).where(Signal.market_id == TEST_CID))
    await session.execute(delete(BookSnapshot).where(BookSnapshot.market_id == TEST_CID))
    await session.execute(delete(Market).where(Market.condition_id == TEST_CID))
    await session.commit()


@pytest.mark.asyncio
async def test_signal_aceptada_genera_paper_fill_y_posicion():
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session)

        session.add(
            Market(
                condition_id=TEST_CID,
                gamma_id="gid_paper_test",
                slug="paper-test",
                question="Test paper fill",
                clob_token_ids=["t1", "t2"],
                outcomes=["Yes", "No"],
            )
        )

        noise = [0, 0.001, -0.001, 0.002, -0.002, 0.001, -0.001, 0, 0.002, -0.001, 0.001, 0]
        now = datetime.now(UTC)
        for i, n in enumerate(noise):
            p = 0.30 + n
            session.add(
                BookSnapshot(
                    market_id=TEST_CID,
                    ts=now - timedelta(seconds=(len(noise) - i) * 30),
                    best_bid=Decimal(str(p - 0.005)),
                    best_ask=Decimal(str(p + 0.005)),
                    last_trade_price=Decimal(str(p)),
                    spread=Decimal("0.01"),
                    volume_24hr=Decimal("5000"),
                    active=True,
                    accepting_orders=True,
                )
            )
        # spike a 0.50
        session.add(
            BookSnapshot(
                market_id=TEST_CID,
                ts=now,
                best_bid=Decimal("0.495"),
                best_ask=Decimal("0.505"),
                last_trade_price=Decimal("0.50"),
                spread=Decimal("0.01"),
                volume_24hr=Decimal("5000"),
                active=True,
                accepting_orders=True,
            )
        )
        await session.commit()

        sig = await evaluate_market(session, TEST_CID)
        await session.commit()
        assert sig is not None and sig.accepted

        fills = (
            await session.execute(select(PaperFill).where(PaperFill.market_id == TEST_CID))
        ).scalars().all()
        assert len(fills) == 1
        fill = fills[0]
        assert fill.side == "BUY_NO"
        assert float(fill.fill_price) > 0.50  # NO debería costar > 0.50 con slippage adverso
        assert float(fill.shares) > 0
        assert float(fill.notional_usd) <= settings.max_risk_per_trade_usd + 0.01

        positions = (
            await session.execute(
                select(PaperPosition).where(PaperPosition.market_id == TEST_CID)
            )
        ).scalars().all()
        assert len(positions) == 1
        pos = positions[0]
        assert pos.side == "BUY_NO"
        assert pos.shares == fill.shares
        assert pos.n_fills == 1
        assert pos.status == "open"

        await _cleanup(session)
