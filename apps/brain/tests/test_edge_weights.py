from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from umbra.analytics.edge_weights import MAX_WEIGHT, MIN_WEIGHT, refresh_edge_weights
from umbra.db.models import EdgePerformance, EdgeWeight
from umbra.db.session import get_sessionmaker

EDGE_A = "edge_weight_a"
EDGE_B = "edge_weight_b"


async def _cleanup(session) -> None:
    await session.execute(delete(EdgeWeight).where(EdgeWeight.edge_name.in_([EDGE_A, EDGE_B])))
    await session.execute(
        delete(EdgePerformance).where(EdgePerformance.edge_name.in_([EDGE_A, EDGE_B]))
    )
    await session.commit()


@pytest.mark.asyncio
async def test_refresh_edge_weights_caps_and_orders_weights():
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session)
        session.add_all(
            [
                EdgePerformance(
                    edge_name=EDGE_A,
                    signals_generated=20,
                    signals_accepted=10,
                    trades_executed=10,
                    wins=8,
                    losses=2,
                    avg_return=Decimal("0.12"),
                    profit_factor=Decimal("3.0"),
                    sharpe=Decimal("1.5"),
                    expectancy=Decimal("4.0"),
                    max_drawdown=Decimal("0.05"),
                    rolling_30d={"trades": 10, "expectancy": 4.0, "profit_factor": 3.0, "sharpe": 1.5},
                    rolling_100_trades={"trades": 10, "expectancy": 4.0, "profit_factor": 3.0, "sharpe": 1.5},
                ),
                EdgePerformance(
                    edge_name=EDGE_B,
                    signals_generated=20,
                    signals_accepted=10,
                    trades_executed=10,
                    wins=3,
                    losses=7,
                    avg_return=Decimal("-0.03"),
                    profit_factor=Decimal("0.6"),
                    sharpe=Decimal("-0.2"),
                    expectancy=Decimal("-1.0"),
                    max_drawdown=Decimal("0.40"),
                    rolling_30d={"trades": 10, "expectancy": -1.0, "profit_factor": 0.6, "sharpe": -0.2},
                    rolling_100_trades={"trades": 10, "expectancy": -1.0, "profit_factor": 0.6, "sharpe": -0.2},
                ),
            ]
        )
        await session.flush()
        rows = await refresh_edge_weights(session)
        await session.commit()

        assert {EDGE_A, EDGE_B}.issubset({row.edge_name for row in rows})
        weights = {
            row.edge_name: row
            for row in (
                await session.execute(select(EdgeWeight))
            ).scalars().all()
        }
        assert MIN_WEIGHT <= float(weights[EDGE_A].weight) <= MAX_WEIGHT
        assert MIN_WEIGHT <= float(weights[EDGE_B].weight) <= MAX_WEIGHT
        assert float(weights[EDGE_A].weight) > float(weights[EDGE_B].weight)
        assert weights[EDGE_A].metadata_json["applied_to_execution"] is False

        await _cleanup(session)
