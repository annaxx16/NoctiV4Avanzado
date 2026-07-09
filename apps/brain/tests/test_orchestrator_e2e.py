"""Test E2E del orchestrator contra la DB real.

Inyecta un Market sintetico + 13 snapshots con overreaction, llama evaluate_market,
verifica que se genero una Signal aceptada. Limpia al final.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from umbra.config import settings

from umbra.db.models import BookSnapshot, Market, Signal, SignalAudit
from umbra.db.session import get_sessionmaker
from umbra.engine.orchestrator import evaluate_market

TEST_CONDITION_ID = "0xtest_orchestrator_e2e_synthetic"


async def _cleanup(session) -> None:
    await session.execute(
        delete(SignalAudit).where(SignalAudit.market_id == TEST_CONDITION_ID)
    )
    await session.execute(delete(Signal).where(Signal.market_id == TEST_CONDITION_ID))
    await session.execute(
        delete(BookSnapshot).where(BookSnapshot.market_id == TEST_CONDITION_ID)
    )
    await session.execute(delete(Market).where(Market.condition_id == TEST_CONDITION_ID))
    await session.commit()


@pytest.mark.asyncio
async def test_orchestrator_emits_accepted_signal_on_synthetic_overreaction():
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session)

        # Market sintetico
        session.add(
            Market(
                condition_id=TEST_CONDITION_ID,
                gamma_id="test_gamma_id",
                slug="synthetic-test",
                question="Synthetic overreaction test",
                clob_token_ids=["t1", "t2"],
                outcomes=["Yes", "No"],
            )
        )

        # 12 snapshots con ruido +/-0.001-0.002 alrededor de 0.30
        noise = [0, 0.001, -0.001, 0.002, -0.002, 0.001, -0.001, 0, 0.002, -0.001, 0.001, 0]
        now = datetime.now(UTC)
        for i, n in enumerate(noise):
            p = 0.30 + n
            session.add(
                BookSnapshot(
                    market_id=TEST_CONDITION_ID,
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
        # spike actual a 0.50 (5 sigma+ con ruido tipico de 0.001)
        session.add(
            BookSnapshot(
                market_id=TEST_CONDITION_ID,
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

        sig = await evaluate_market(session, TEST_CONDITION_ID)
        await session.commit()

        assert sig is not None, "el orchestrator debe generar una senal"
        assert sig.side == "BUY_NO", f"esperaba BUY_NO, recibi {sig.side}"
        assert sig.accepted, f"esperaba accepted=True, razon: {sig.reason}"
        assert sig.notional_usd is not None and sig.notional_usd > 0
        assert sig.notional_usd <= Decimal(str(settings.max_risk_per_trade_usd + 0.01)), (
            "max_risk_per_trade_usd deberia capear al valor configurado"
        )
        audit = (
            await session.execute(
                select(SignalAudit).where(SignalAudit.signal_id == sig.id)
            )
        ).scalar_one()
        assert audit.accepted is True
        assert audit.rejected is False
        assert audit.edge_name == "overreaction_v1"
        assert audit.direction == "BUY_NO"
        assert audit.rejected_reason is None

        await _cleanup(session)
