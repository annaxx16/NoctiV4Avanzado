"""Tests del Exit Engine + close_position + no-averaging-down + flatten."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from umbra.db.models import (
    BookSnapshot,
    EquitySnapshot,
    Market,
    Outcome,
    PaperFill,
    PaperPosition,
    Signal,
)
from umbra.db.session import get_sessionmaker
from umbra.engine.exit_engine import (
    evaluate_and_execute_exits,
    flatten_all,
)
from umbra.execution.paper import execute_close
from umbra.portfolio.manager import persist_equity_snapshot
from umbra.risk.engine import check as risk_check
from umbra.risk.sizer import SizingResult


def _cid(suffix: str) -> str:
    return f"0xtest_exit_{suffix}"


async def _seed_market(session, cid: str) -> None:
    session.add(
        Market(
            condition_id=cid,
            gamma_id=f"gid_{cid[-12:]}",
            slug=f"slug-{cid[-12:]}",
            question=f"Test {cid[-12:]}",
            clob_token_ids=["t1", "t2"],
            outcomes=["Yes", "No"],
            end_date=datetime.now(UTC) + timedelta(days=7),
        )
    )


async def _seed_snapshots_flat(session, cid: str, base: float, n: int = 12) -> None:
    """N snapshots planos alrededor de `base` (sin spike)."""
    now = datetime.now(UTC)
    noise = [0, 0.001, -0.001, 0.002, -0.002, 0.001, -0.001, 0, 0.002, -0.001, 0.001, 0]
    for i in range(n):
        p = base + noise[i % len(noise)]
        session.add(
            BookSnapshot(
                market_id=cid,
                ts=now - timedelta(seconds=(n - i) * 30),
                best_bid=Decimal(str(p - 0.005)),
                best_ask=Decimal(str(p + 0.005)),
                last_trade_price=Decimal(str(p)),
                spread=Decimal("0.01"),
                volume_24hr=Decimal("5000"),
                active=True,
                accepting_orders=True,
            )
        )


async def _cleanup(session, cid: str) -> None:
    await session.execute(delete(PaperFill).where(PaperFill.market_id == cid))
    await session.execute(delete(PaperPosition).where(PaperPosition.market_id == cid))
    await session.execute(delete(Signal).where(Signal.market_id == cid))
    await session.execute(delete(Outcome).where(Outcome.market_id == cid))
    await session.execute(delete(BookSnapshot).where(BookSnapshot.market_id == cid))
    await session.execute(delete(Market).where(Market.condition_id == cid))
    await session.commit()


async def _full_cleanup_equity(session) -> None:
    await session.execute(delete(EquitySnapshot))
    await session.commit()


# ---------------------------------------------------------------------------
# 1. close_position genera realized PnL coherente y cierra
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_close_produces_realized_pnl_and_closes_position():
    cid = _cid("close_basic")
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session, cid)
        await _seed_market(session, cid)
        # Posición sintética BUY_YES: 100 shares con avg 0.40 → cost 40.
        session.add(
            PaperPosition(
                market_id=cid,
                side="BUY_YES",
                shares=Decimal("100"),
                avg_entry_price=Decimal("0.40"),
                total_cost_usd=Decimal("40"),
                total_fees_usd=Decimal("0"),
                realized_pnl_usd=Decimal("0"),
                peak_unrealized_pnl_usd=Decimal("0"),
                n_fills=1,
                status="open",
            )
        )
        await session.commit()

        pos = (
            await session.execute(
                select(PaperPosition).where(PaperPosition.market_id == cid)
            )
        ).scalar_one()

        res = await execute_close(
            session=session,
            position=pos,
            current_mid_yes=0.60,
            liquidity_usd=10_000.0,
            fraction=1.0,
            reason="manual_test",
        )
        await session.commit()

        assert res is not None
        assert res.action == "CLOSE"
        # con mid_yes=0.60 para BUY_YES, side_price=0.60 menos slippage.
        # realized debe ser POSITIVO (compré a 0.40, vendo cerca de 0.60).
        assert res.realized_pnl_usd > 0
        # posición debe estar cerrada
        pos2 = (
            await session.execute(
                select(PaperPosition).where(PaperPosition.market_id == cid)
            )
        ).scalar_one()
        assert pos2.status == "closed"
        assert float(pos2.shares) == 0
        assert float(pos2.realized_pnl_usd) > 0

        await _cleanup(session, cid)


# ---------------------------------------------------------------------------
# 2. No averaging down: con posición abierta misma (market,side) → rechaza
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_engine_blocks_averaging_down_same_market_side():
    cid = _cid("no_avg")
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session, cid)
        await _seed_market(session, cid)
        await _seed_snapshots_flat(session, cid, base=0.30)
        # Posición ya abierta del mismo lado:
        session.add(
            PaperPosition(
                market_id=cid,
                side="BUY_NO",
                shares=Decimal("50"),
                avg_entry_price=Decimal("0.60"),
                total_cost_usd=Decimal("30"),
                total_fees_usd=Decimal("0"),
                realized_pnl_usd=Decimal("0"),
                peak_unrealized_pnl_usd=Decimal("0"),
                n_fills=1,
                status="open",
            )
        )
        await session.commit()

        # Sizing válido a propósito:
        sz = SizingResult(f_star=0.5, shares=10.0, notional_usd=4.0)
        d = await risk_check(
            session,
            condition_id=cid,
            edge_value=0.10,
            sizing=sz,
            side="BUY_NO",
        )
        assert d.accepted is False
        assert "position_already_open" in d.reason

        await _cleanup(session, cid)


# ---------------------------------------------------------------------------
# 3. Stop loss trigger: PnL <= -SL_PCT → exit engine cierra
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_engine_triggers_stop_loss():
    cid = _cid("sl")
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session, cid)
        await _seed_market(session, cid)
        # Snapshot reciente que implica mid_yes=0.30 → side BUY_NO price=0.70 favorable
        # Para forzar pérdida en BUY_NO: hacer que el current price del NO sea bajo.
        # avg_entry=0.70 → si mid_yes=0.85 → no_price=0.15 → pnl_pct = (15-70)/70 ≈ -78%
        now = datetime.now(UTC)
        session.add(
            BookSnapshot(
                market_id=cid,
                ts=now,
                best_bid=Decimal("0.845"),
                best_ask=Decimal("0.855"),
                last_trade_price=Decimal("0.85"),
                spread=Decimal("0.01"),
                liquidity_num=Decimal("10000"),
                volume_24hr=Decimal("10000"),
                active=True,
                accepting_orders=True,
            )
        )
        session.add(
            PaperPosition(
                market_id=cid,
                side="BUY_NO",
                shares=Decimal("100"),
                avg_entry_price=Decimal("0.70"),
                total_cost_usd=Decimal("70"),
                total_fees_usd=Decimal("0"),
                realized_pnl_usd=Decimal("0"),
                peak_unrealized_pnl_usd=Decimal("0"),
                n_fills=1,
                status="open",
                opened_at=now - timedelta(minutes=30),
            )
        )
        await session.commit()

        decisions = await evaluate_and_execute_exits(session, portfolio_dd_pct=0.0)
        await session.commit()

        assert len(decisions) == 1
        assert decisions[0].reason == "t4_stop_loss"
        # Posición cerrada con realized NEGATIVO
        pos = (
            await session.execute(
                select(PaperPosition).where(PaperPosition.market_id == cid)
            )
        ).scalar_one()
        assert pos.status == "closed"
        assert float(pos.realized_pnl_usd) < 0

        await _cleanup(session, cid)


# ---------------------------------------------------------------------------
# 4. Time stop: age > TTL → cerrar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_engine_triggers_time_stop():
    cid = _cid("ttl")
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session, cid)
        await _seed_market(session, cid)
        now = datetime.now(UTC)
        session.add(
            BookSnapshot(
                market_id=cid,
                ts=now,
                best_bid=Decimal("0.495"),
                best_ask=Decimal("0.505"),
                last_trade_price=Decimal("0.50"),
                spread=Decimal("0.01"),
                liquidity_num=Decimal("10000"),
                volume_24hr=Decimal("10000"),
                active=True,
                accepting_orders=True,
            )
        )
        session.add(
            PaperPosition(
                market_id=cid,
                side="BUY_YES",
                shares=Decimal("100"),
                avg_entry_price=Decimal("0.50"),
                total_cost_usd=Decimal("50"),
                total_fees_usd=Decimal("0"),
                realized_pnl_usd=Decimal("0"),
                peak_unrealized_pnl_usd=Decimal("0"),
                n_fills=1,
                status="open",
                opened_at=now - timedelta(hours=24),  # muy viejo
            )
        )
        await session.commit()

        decisions = await evaluate_and_execute_exits(session, portfolio_dd_pct=0.0)
        await session.commit()

        assert len(decisions) == 1
        # Cualquiera de stop_loss, take_profit, time_stop o trailing. Con avg=0.5
        # y mid=0.5: pnl ≈ 0 → debería ser TIME_STOP.
        assert decisions[0].reason == "t7_time_stop"
        await _cleanup(session, cid)


# ---------------------------------------------------------------------------
# 5. Take profit: pnl_pct >= TP_PCT → cerrar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_engine_triggers_take_profit():
    cid = _cid("tp")
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session, cid)
        await _seed_market(session, cid)
        now = datetime.now(UTC)
        # BUY_YES avg=0.40, mid=0.55 → side_price=0.55 → pnl_pct ≈ +37% > 25%
        session.add(
            BookSnapshot(
                market_id=cid,
                ts=now,
                best_bid=Decimal("0.545"),
                best_ask=Decimal("0.555"),
                last_trade_price=Decimal("0.55"),
                spread=Decimal("0.01"),
                liquidity_num=Decimal("10000"),
                volume_24hr=Decimal("10000"),
                active=True,
                accepting_orders=True,
            )
        )
        session.add(
            PaperPosition(
                market_id=cid,
                side="BUY_YES",
                shares=Decimal("100"),
                avg_entry_price=Decimal("0.40"),
                total_cost_usd=Decimal("40"),
                total_fees_usd=Decimal("0"),
                realized_pnl_usd=Decimal("0"),
                peak_unrealized_pnl_usd=Decimal("0"),
                n_fills=1,
                status="open",
                opened_at=now - timedelta(minutes=10),
            )
        )
        await session.commit()

        decisions = await evaluate_and_execute_exits(session, portfolio_dd_pct=0.0)
        await session.commit()

        assert len(decisions) == 1
        assert decisions[0].reason == "t8_take_profit"
        pos = (
            await session.execute(
                select(PaperPosition).where(PaperPosition.market_id == cid)
            )
        ).scalar_one()
        assert pos.status == "closed"
        assert float(pos.realized_pnl_usd) > 0
        await _cleanup(session, cid)


# ---------------------------------------------------------------------------
# 6. Outcome resuelto → close al outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exit_engine_closes_resolved_market():
    cid = _cid("outcome")
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session, cid)
        await _seed_market(session, cid)
        now = datetime.now(UTC)
        # Outcome YES=True. BUY_NO pierde todo.
        session.add(
            Outcome(
                market_id=cid,
                resolved_at=now,
                yes_outcome=True,
                source="manual_test",
            )
        )
        session.add(
            BookSnapshot(
                market_id=cid,
                ts=now,
                best_bid=Decimal("0.95"),
                best_ask=Decimal("0.99"),
                last_trade_price=Decimal("0.97"),
                spread=Decimal("0.04"),
                liquidity_num=Decimal("10000"),
                volume_24hr=Decimal("10000"),
                active=True,
                accepting_orders=True,
            )
        )
        session.add(
            PaperPosition(
                market_id=cid,
                side="BUY_NO",
                shares=Decimal("100"),
                avg_entry_price=Decimal("0.40"),
                total_cost_usd=Decimal("40"),
                total_fees_usd=Decimal("0"),
                realized_pnl_usd=Decimal("0"),
                peak_unrealized_pnl_usd=Decimal("0"),
                n_fills=1,
                status="open",
                opened_at=now - timedelta(minutes=15),
            )
        )
        await session.commit()

        decisions = await evaluate_and_execute_exits(session, portfolio_dd_pct=0.0)
        await session.commit()

        assert len(decisions) == 1
        assert decisions[0].reason == "t1_outcome_resolved"
        await _cleanup(session, cid)


# ---------------------------------------------------------------------------
# 7. flatten_all cierra todas las posiciones abiertas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flatten_all_closes_every_open_position():
    cid_a = _cid("flat_a")
    cid_b = _cid("flat_b")
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session, cid_a)
        await _cleanup(session, cid_b)
        await _seed_market(session, cid_a)
        await _seed_market(session, cid_b)
        now = datetime.now(UTC)
        for cid in (cid_a, cid_b):
            session.add(
                BookSnapshot(
                    market_id=cid,
                    ts=now,
                    best_bid=Decimal("0.495"),
                    best_ask=Decimal("0.505"),
                    last_trade_price=Decimal("0.50"),
                    spread=Decimal("0.01"),
                    liquidity_num=Decimal("10000"),
                    volume_24hr=Decimal("10000"),
                    active=True,
                    accepting_orders=True,
                )
            )
            session.add(
                PaperPosition(
                    market_id=cid,
                    side="BUY_YES",
                    shares=Decimal("100"),
                    avg_entry_price=Decimal("0.50"),
                    total_cost_usd=Decimal("50"),
                    total_fees_usd=Decimal("0"),
                    realized_pnl_usd=Decimal("0"),
                    peak_unrealized_pnl_usd=Decimal("0"),
                    n_fills=1,
                    status="open",
                    opened_at=now - timedelta(minutes=10),
                )
            )
        await session.commit()

        n = await flatten_all(session, reason="test_flatten")
        await session.commit()
        assert n == 2
        open_positions = (
            await session.execute(
                select(PaperPosition).where(PaperPosition.status == "open")
            )
        ).scalars().all()
        # No deberíamos tener posiciones abiertas (al menos no las nuestras)
        for p in open_positions:
            assert p.market_id not in (cid_a, cid_b)

        await _cleanup(session, cid_a)
        await _cleanup(session, cid_b)


# ---------------------------------------------------------------------------
# 8. Equity curve REAL: persist_equity_snapshot crea fila con equity correcta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_equity_snapshot_persists_real_equity():
    cid = _cid("equity")
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session, cid)
        await _seed_market(session, cid)
        # Sin posiciones → equity = bankroll, DD = 0.
        snap_before = await persist_equity_snapshot(session)
        await session.commit()
        assert float(snap_before.equity_usd) > 0
        assert float(snap_before.drawdown_pct) == 0.0

        await _cleanup(session, cid)


@pytest.mark.asyncio
async def test_flat_portfolio_resets_drawdown_cycle_after_old_peak():
    cid = _cid("dd_reset")
    sm = get_sessionmaker()
    async with sm() as session:
        await _cleanup(session, cid)
        await _seed_market(session, cid)

        open_count = (
            await session.execute(
                select(func.count()).select_from(PaperPosition).where(
                    PaperPosition.status == "open",
                    PaperPosition.shares > 0,
                    PaperPosition.market_id != cid,
                )
            )
        ).scalar()
        if open_count:
            pytest.skip("requires a flat portfolio in the shared integration DB")

        now = datetime.now(UTC)
        old_peak = EquitySnapshot(
            ts=now - timedelta(hours=2),
            cash_usd=Decimal("2500"),
            positions_value_usd=Decimal("0"),
            equity_usd=Decimal("2500"),
            unrealized_pnl_usd=Decimal("0"),
            realized_pnl_usd_total=Decimal("1500"),
            gross_exposure_usd=Decimal("0"),
            peak_equity_usd=Decimal("2500"),
            drawdown_pct=Decimal("0"),
            n_open_positions=0,
        )
        session.add(old_peak)
        session.add(
            PaperPosition(
                market_id=cid,
                side="BUY_YES",
                shares=Decimal("0"),
                avg_entry_price=Decimal("0"),
                total_cost_usd=Decimal("0"),
                total_fees_usd=Decimal("0"),
                realized_pnl_usd=Decimal("-200"),
                peak_unrealized_pnl_usd=Decimal("0"),
                n_fills=1,
                status="closed",
                opened_at=now - timedelta(hours=1),
                closed_at=now - timedelta(minutes=30),
            )
        )
        await session.commit()

        snap = await persist_equity_snapshot(session)
        await session.commit()

        assert float(snap.peak_equity_usd) == pytest.approx(float(snap.equity_usd))
        assert float(snap.drawdown_pct) == 0.0

        await session.execute(
            delete(EquitySnapshot).where(EquitySnapshot.id.in_([old_peak.id, snap.id]))
        )
        await session.commit()
        await _cleanup(session, cid)
