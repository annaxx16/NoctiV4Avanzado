"""Feature calculator: funciones puras sobre snapshots ordenados.

Reglas anti-lookahead:
- `as_of` define el tiempo de cálculo. NUNCA se usan snapshots con ts > as_of.
- Si falta data para una feature, se devuelve None (no se extrapola).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class SnapshotInput:
    ts: datetime
    best_bid: float | None
    best_ask: float | None
    last_trade_price: float | None
    spread: float | None
    volume_24hr: float | None


@dataclass(frozen=True)
class FeatureSet:
    as_of: datetime
    mid_price: float | None
    spread: float | None
    delta_p_1m: float | None
    delta_p_5m: float | None
    spread_expansion: float | None
    vol_z: float | None
    mid_velocity: float | None
    n_snapshots: int

    def as_dict(self) -> dict[str, float | int | str | None]:
        return {
            "as_of": self.as_of.isoformat(),
            "mid_price": self.mid_price,
            "spread": self.spread,
            "delta_p_1m": self.delta_p_1m,
            "delta_p_5m": self.delta_p_5m,
            "spread_expansion": self.spread_expansion,
            "vol_z": self.vol_z,
            "mid_velocity": self.mid_velocity,
            "n_snapshots": self.n_snapshots,
        }


def _mid(snap: SnapshotInput) -> float | None:
    if snap.best_bid is None or snap.best_ask is None:
        return snap.last_trade_price
    return (snap.best_bid + snap.best_ask) / 2.0


def _filter_no_lookahead(
    snapshots: Iterable[SnapshotInput], as_of: datetime
) -> list[SnapshotInput]:
    return sorted(
        (s for s in snapshots if s.ts <= as_of), key=lambda s: s.ts
    )


def _value_at_or_before(
    snapshots: list[SnapshotInput], target_ts: datetime
) -> SnapshotInput | None:
    candidate: SnapshotInput | None = None
    for s in snapshots:
        if s.ts <= target_ts:
            candidate = s
        else:
            break
    return candidate


def _delta_mid(
    snapshots: list[SnapshotInput], as_of: datetime, lookback: timedelta
) -> float | None:
    current = snapshots[-1] if snapshots else None
    if current is None:
        return None
    current_mid = _mid(current)
    if current_mid is None:
        return None
    past = _value_at_or_before(snapshots[:-1], as_of - lookback)
    if past is None:
        return None
    past_mid = _mid(past)
    if past_mid is None:
        return None
    return current_mid - past_mid


def _spread_expansion(snapshots: list[SnapshotInput], as_of: datetime) -> float | None:
    """z-score del spread actual vs el rolling de los últimos 5 min anteriores."""
    if not snapshots:
        return None
    current = snapshots[-1]
    if current.spread is None:
        return None
    window_start = as_of - timedelta(minutes=5)
    history = [
        s.spread
        for s in snapshots[:-1]
        if s.ts >= window_start and s.spread is not None
    ]
    if len(history) < 5:
        return None
    mean_ = statistics.mean(history)
    try:
        std_ = statistics.stdev(history)
    except statistics.StatisticsError:
        return None
    if std_ == 0 or math.isnan(std_):
        return None
    return (current.spread - mean_) / std_


def _vol_z(snapshots: list[SnapshotInput], as_of: datetime) -> float | None:
    if not snapshots:
        return None
    current = snapshots[-1]
    if current.volume_24hr is None:
        return None
    window_start = as_of - timedelta(minutes=30)
    history = [
        s.volume_24hr
        for s in snapshots[:-1]
        if s.ts >= window_start and s.volume_24hr is not None
    ]
    if len(history) < 5:
        return None
    mean_ = statistics.mean(history)
    try:
        std_ = statistics.stdev(history)
    except statistics.StatisticsError:
        return None
    if std_ == 0:
        return None
    return (current.volume_24hr - mean_) / std_


def _mid_velocity(snapshots: list[SnapshotInput]) -> float | None:
    """Derivada simple: (mid(t) - mid(t-1)) / dt_seconds. Usa el snapshot
    inmediato anterior, no una ventana temporal fija — porque el poller
    espacia ~30s pero no exacto."""
    if len(snapshots) < 2:
        return None
    last, prev = snapshots[-1], snapshots[-2]
    mid_last = _mid(last)
    mid_prev = _mid(prev)
    if mid_last is None or mid_prev is None:
        return None
    dt = (last.ts - prev.ts).total_seconds()
    if dt <= 0:
        return None
    return (mid_last - mid_prev) / dt


def calculate_features(
    snapshots: Iterable[SnapshotInput], as_of: datetime
) -> FeatureSet:
    history = _filter_no_lookahead(snapshots, as_of)

    if not history:
        return FeatureSet(
            as_of=as_of,
            mid_price=None,
            spread=None,
            delta_p_1m=None,
            delta_p_5m=None,
            spread_expansion=None,
            vol_z=None,
            mid_velocity=None,
            n_snapshots=0,
        )

    current = history[-1]
    return FeatureSet(
        as_of=as_of,
        mid_price=_mid(current),
        spread=current.spread,
        delta_p_1m=_delta_mid(history, as_of, timedelta(minutes=1)),
        delta_p_5m=_delta_mid(history, as_of, timedelta(minutes=5)),
        spread_expansion=_spread_expansion(history, as_of),
        vol_z=_vol_z(history, as_of),
        mid_velocity=_mid_velocity(history),
        n_snapshots=len(history),
    )
