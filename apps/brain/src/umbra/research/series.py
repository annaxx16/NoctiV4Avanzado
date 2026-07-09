"""Serie de precios genérica: el puente entre dominios.

`PricePoint` es el átomo común. Un mid de Polymarket y un close de un activo
continuo se reducen ambos a `(ts, value)` con `value > 0`. Toda la capa de
régimen y drawdown opera sobre esta serie, así que es agnóstica al dominio.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from umbra.features.calculator import SnapshotInput


@dataclass(frozen=True)
class PricePoint:
    ts: datetime
    value: float


def _mid(snap: SnapshotInput) -> float | None:
    """Mid de Polymarket, con el mismo fallback que el feature calculator."""
    if snap.best_bid is None or snap.best_ask is None:
        return snap.last_trade_price
    return (snap.best_bid + snap.best_ask) / 2.0


def snapshots_to_series(snapshots: Iterable[SnapshotInput]) -> list[PricePoint]:
    """Adapter Polymarket → serie genérica. Descarta snapshots sin mid y los
    de valor no positivo (un mid 0 rompería el drawdown relativo)."""
    points: list[PricePoint] = []
    for s in sorted(snapshots, key=lambda x: x.ts):
        v = _mid(s)
        if v is not None and v > 0:
            points.append(PricePoint(ts=s.ts, value=v))
    return points


def returns(points: list[PricePoint], *, eps: float = 1e-9) -> list[float]:
    """Retornos relativos punto a punto: (vₜ - vₜ₋₁) / max(|vₜ₋₁|, eps).

    Relativos (no absolutos) para que la volatilidad sea comparable entre un mid
    ∈ [0,1] y un precio de tres cifras. El `eps` evita división por ~0.
    """
    out: list[float] = []
    for prev, cur in zip(points, points[1:], strict=False):
        out.append((cur.value - prev.value) / max(abs(prev.value), eps))
    return out
