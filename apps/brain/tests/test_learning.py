from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from umbra.analytics.learning import run_learning_once
from umbra.db.models import EdgePerformance, EdgeWeight, LearningSnapshot, Market, SignalAudit
from umbra.db.session import get_sessionmaker

EDGE = "learning_edge"
CID = "0xtest_learning_edge"


async def _cleanup(session) -> None:
    await session.execute(delete(EdgeWeight).where(EdgeWeight.edge_name == EDGE))
    await session.execute(delete(EdgePerformance).where(EdgePerformance.edge_name == EDGE))
    await session.execute(delete(SignalAudit).where(SignalAudit.market_id == CID))
    await session.execute(delete(Market).where(Market.condition_id == CID))
    rows = (
        await session.execute(select(LearningSnapshot))
    ).scalars().all()
    for row in rows:
        report = row.report_json or {}
        names = {item.get("edge_name") for item in report.get("performance", [])}
        if EDGE in names:
            await session.delete(row)
    await session.commit()


@pytest.mark.asyncio
async def test_run_learning_once_refreshes_weights_and_records_snapshot():
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session)
        session.add(
            Market(
                condition_id=CID,
                gamma_id="gid_learning_edge",
                slug="learning-edge",
                question="Learning edge test",
                clob_token_ids=["t1", "t2"],
                outcomes=["Yes", "No"],
            )
        )
        session.add(
            SignalAudit(
                market_id=CID,
                market_name="Learning edge test",
                edge_name=EDGE,
                score=Decimal("0.10"),
                direction="BUY_YES",
                accepted=True,
                rejected=False,
                metadata_json={"test": True},
            )
        )
        await session.flush()

        snap = await run_learning_once(session)
        await session.commit()

        assert snap.status == "ok"
        assert snap.edges_evaluated >= 1
        assert snap.weights_updated >= 1
        assert snap.report_json["weights"]

        weight = (
            await session.execute(select(EdgeWeight).where(EdgeWeight.edge_name == EDGE))
        ).scalar_one()
        assert float(weight.weight) >= 0.05
        assert weight.metadata_json["applied_to_execution"] is False

        await _cleanup(session)
