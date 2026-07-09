"""Soportes y resistencias — detección por swings + clustering.

Algoritmo:
1. Pivot detection: un bar i es swing_high si `high[i]` es máximo en una ventana
   simétrica de `window` bars; análogo para swing_low.
2. Clustering: niveles cercanos (distancia < tolerance) se colapsan en un único
   nivel cuyo valor es la media de los touches y cuya fuerza es el conteo.
3. Filtrado: niveles con `touches < min_touches` se descartan.

Devuelve resistance_levels (sobre el último close) y support_levels (debajo),
ordenados por proximidad y fuerza.
"""

from __future__ import annotations

from dataclasses import dataclass

from umbra.ta.ohlc import Bar


@dataclass(frozen=True)
class Level:
    price: float
    touches: int
    kind: str  # 'support' | 'resistance' | 'pivot'


def _find_pivots(bars: list[Bar], window: int = 3) -> tuple[list[int], list[int]]:
    """Devuelve (indices_swing_high, indices_swing_low)."""
    n = len(bars)
    swing_highs, swing_lows = [], []
    if n < 2 * window + 1:
        return swing_highs, swing_lows
    for i in range(window, n - window):
        hi = bars[i].high
        lo = bars[i].low
        is_high = all(hi >= bars[j].high for j in range(i - window, i + window + 1) if j != i)
        is_low = all(lo <= bars[j].low for j in range(i - window, i + window + 1) if j != i)
        if is_high:
            swing_highs.append(i)
        if is_low:
            swing_lows.append(i)
    return swing_highs, swing_lows


def _cluster(prices: list[float], tolerance: float) -> list[tuple[float, int]]:
    """Agrupa precios cercanos. Devuelve [(price_avg, count), ...]."""
    if not prices:
        return []
    sorted_p = sorted(prices)
    clusters: list[list[float]] = [[sorted_p[0]]]
    for p in sorted_p[1:]:
        if abs(p - clusters[-1][-1]) <= tolerance:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [(sum(c) / len(c), len(c)) for c in clusters]


def detect_levels(
    bars: list[Bar],
    window: int = 3,
    tolerance: float = 0.015,
    min_touches: int = 2,
) -> list[Level]:
    """Detección global de niveles. Sin distinguir soporte vs resistencia (pivot)."""
    if not bars:
        return []
    swing_h_idx, swing_l_idx = _find_pivots(bars, window=window)
    highs = [bars[i].high for i in swing_h_idx]
    lows = [bars[i].low for i in swing_l_idx]

    all_prices = highs + lows
    clusters = _cluster(all_prices, tolerance=tolerance)

    levels: list[Level] = []
    for price, touches in clusters:
        if touches < min_touches:
            continue
        levels.append(Level(price=price, touches=touches, kind="pivot"))
    return levels


@dataclass(frozen=True)
class LevelsView:
    last_close: float
    supports: list[Level]      # niveles por DEBAJO del last_close, ordenados desc (más cercano primero)
    resistances: list[Level]   # niveles por ENCIMA, ordenados asc
    nearest_support: Level | None
    nearest_resistance: Level | None


def classify_levels(
    bars: list[Bar],
    window: int = 3,
    tolerance: float = 0.015,
    min_touches: int = 2,
) -> LevelsView:
    if not bars:
        return LevelsView(0.0, [], [], None, None)
    last_close = bars[-1].close
    all_levels = detect_levels(bars, window, tolerance, min_touches)
    supports = sorted(
        (Level(lv.price, lv.touches, "support") for lv in all_levels if lv.price < last_close),
        key=lambda lv: -lv.price,  # más cercanos primero (desc)
    )
    resistances = sorted(
        (Level(lv.price, lv.touches, "resistance") for lv in all_levels if lv.price > last_close),
        key=lambda lv: lv.price,  # más cercanos primero (asc)
    )
    return LevelsView(
        last_close=last_close,
        supports=supports,
        resistances=resistances,
        nearest_support=supports[0] if supports else None,
        nearest_resistance=resistances[0] if resistances else None,
    )
