"""MomentumV1: small directional fallback edge.

Overreaction is selective. This edge lets the bot participate when recent mid
price drift is clear enough but not extreme enough to trigger mean reversion.
It still goes through TA, risk, sizing, liquidity, exposure, and exit gates.
"""

from __future__ import annotations

from datetime import datetime

from umbra.config import settings
from umbra.edges.common import mid as _mid
from umbra.edges.overreaction import EdgeOutput
from umbra.features.calculator import SnapshotInput


def detect(
    snapshots: list[SnapshotInput],
    as_of: datetime,
    *,
    min_delta: float | None = None,
    lookback_snapshots: int | None = None,
) -> EdgeOutput | None:
    if not settings.enable_momentum_edge:
        return None

    min_delta = min_delta if min_delta is not None else settings.momentum_min_delta
    lookback_snapshots = (
        lookback_snapshots
        if lookback_snapshots is not None
        else settings.momentum_lookback_snapshots
    )

    history = sorted((s for s in snapshots if s.ts <= as_of), key=lambda s: s.ts)
    mids = [m for m in (_mid(s) for s in history) if m is not None]
    if len(mids) <= lookback_snapshots:
        return None

    market_price = mids[-1]
    previous = mids[-1 - lookback_snapshots]
    delta = market_price - previous
    if abs(delta) < min_delta:
        return None
    if not (0.01 <= market_price <= 0.99):
        return None

    if delta > 0:
        side = "BUY_YES"
        fair_price = min(0.999, market_price + abs(delta))
    else:
        side = "BUY_NO"
        fair_price = max(0.001, market_price - abs(delta))

    return EdgeOutput(
        edge_name="momentum_v1",
        side=side,
        market_price=market_price,
        fair_price=fair_price,
        edge_value=abs(fair_price - market_price),
        strength=delta / max(min_delta, 0.000001),
        reason=(
            f"mid={market_price:.4f} prev={previous:.4f} "
            f"delta={delta:+.4f}"
        ),
        as_of=as_of,
    )
