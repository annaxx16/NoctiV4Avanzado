"""Anti-lookahead obligatorio para la capa de régimen.

Si `extract_regime_features` usara puntos con ts > as_of, el etiquetado de
régimen estaría contaminado con el futuro y cualquier análisis condicionado a él
sería mentira (igual que en los features de producción).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from umbra.research.regime import extract_regime_features
from umbra.research.series import PricePoint

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _pt(minutes_ago: int, value: float) -> PricePoint:
    return PricePoint(ts=BASE - timedelta(minutes=minutes_ago), value=value)


def test_future_points_are_ignored():
    past = [_pt(8, 0.50), _pt(6, 0.50), _pt(4, 0.50), _pt(2, 0.50), _pt(0, 0.50)]
    future = PricePoint(ts=BASE + timedelta(minutes=1), value=0.99)  # shock futuro

    f_clean = extract_regime_features(past, BASE, window=timedelta(minutes=15))
    f_dirty = extract_regime_features([*past, future], BASE, window=timedelta(minutes=15))

    assert f_clean is not None and f_dirty is not None
    # el punto futuro nunca debe alterar volatilidad, drift ni conteo
    assert f_dirty.n == f_clean.n
    assert f_dirty.volatility == pytest.approx(f_clean.volatility)
    assert f_dirty.drift == pytest.approx(f_clean.drift)


def test_window_excludes_points_before_start():
    pts = [_pt(120, 0.10), _pt(5, 0.50), _pt(3, 0.51), _pt(1, 0.50)]
    # ventana de 10 min: el punto de hace 120 min queda fuera
    f = extract_regime_features(pts, BASE, window=timedelta(minutes=10))
    assert f is not None
    assert f.n == 3  # solo los tres recientes
