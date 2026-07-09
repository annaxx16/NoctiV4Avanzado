"""OverreactionV1: detecta sobre-reacciones del precio respecto a su EMA.

Idea: si el mid_price está N desviaciones estándar (recientes) por encima/debajo de
su EMA, asumimos que el "fair" es la EMA y que el mercado va a revertir.

Salida:
  - side BUY_NO si mid > fair (precio inflado → apostar que baja)
  - side BUY_YES si mid < fair
  - None si la magnitud es insuficiente o falta historia
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime

from umbra.config import settings
from umbra.edges.common import ema as _ema
from umbra.edges.common import mid as _mid
from umbra.features.calculator import SnapshotInput


@dataclass(frozen=True)
class EdgeOutput:
    edge_name: str
    side: str  # BUY_YES | BUY_NO
    market_price: float
    fair_price: float
    edge_value: float  # |fair - market|, en unidades de probabilidad (0..1)
    strength: float  # número de sigmas
    reason: str
    as_of: datetime


def detect(
    snapshots: list[SnapshotInput],
    as_of: datetime,
    *,
    sigma_threshold: float | None = None,
    ema_alpha: float | None = None,
    min_snapshots: int | None = None,
) -> EdgeOutput | None:
    """Detecta overreaction.

    Los parámetros opcionales permiten al backtester barrer hiperparámetros
    (análisis de sensibilidad) sin tocar `settings`. Si se omiten, usan los
    valores de configuración — comportamiento idéntico al de producción.
    """
    sigma_threshold = (
        sigma_threshold if sigma_threshold is not None
        else settings.overreaction_sigma_threshold
    )
    ema_alpha = ema_alpha if ema_alpha is not None else settings.ema_alpha
    min_snapshots = (
        min_snapshots if min_snapshots is not None
        else settings.overreaction_min_snapshots
    )

    history = sorted(
        (s for s in snapshots if s.ts <= as_of), key=lambda s: s.ts
    )

    mids = [m for m in (_mid(s) for s in history) if m is not None]
    if len(mids) < min_snapshots:
        return None

    market_price = mids[-1]
    # EMA computada sobre el historial SIN incluir el punto actual,
    # para que el "fair" represente la tendencia previa a la decisión.
    history_without_current = mids[:-1]
    if len(history_without_current) < min_snapshots:
        return None
    fair_price = _ema(history_without_current, ema_alpha)

    # Std del ruido base: los últimos N puntos ANTES del actual.
    # Si incluyésemos market_price, su propia magnitud inflaría el std
    # y enmascararía la overreaction.
    recent = history_without_current[-min_snapshots:]
    try:
        recent_std = statistics.stdev(recent)
    except statistics.StatisticsError:
        return None
    if recent_std <= 0:
        return None

    sigma = (market_price - fair_price) / recent_std
    if abs(sigma) < sigma_threshold:
        return None

    if not (0.01 <= market_price <= 0.99):
        return None

    side = "BUY_NO" if sigma > 0 else "BUY_YES"
    return EdgeOutput(
        edge_name="overreaction_v1",
        side=side,
        market_price=market_price,
        fair_price=fair_price,
        edge_value=abs(fair_price - market_price),
        strength=sigma,
        reason=f"mid={market_price:.4f} vs ema={fair_price:.4f}, sigma={sigma:+.2f}",
        as_of=as_of,
    )
