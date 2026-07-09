"""Detección de tendencia + canal sobre OHLC bars.

Output principal: TrendInfo con
  regime: 'BULL' | 'BEAR' | 'RANGE'
  ema_fast, ema_slow
  slope: pendiente de regresión lineal sobre N últimos closes (per-bar)
  channel_high, channel_low, channel_mid: envolventes de Donchian
  channel_width_pct: ancho relativo (proxy de volatilidad)
  position_in_channel: 0 (en floor) .. 1 (en techo)

Reglas de régimen:
  BULL  si ema_fast > ema_slow * (1 + threshold) Y slope > slope_threshold
  BEAR  si ema_fast < ema_slow * (1 - threshold) Y slope < -slope_threshold
  RANGE en cualquier otro caso
"""

from __future__ import annotations

from dataclasses import dataclass

from umbra.ta.ohlc import Bar


def _ema(values: list[float], period: int) -> float | None:
    if not values or period <= 0:
        return None
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = alpha * v + (1 - alpha) * e
    return e


def _linear_slope(values: list[float]) -> float:
    """Pendiente OLS por bar. Si len<2 → 0."""
    n = len(values)
    if n < 2:
        return 0.0
    sx = sum(range(n))
    sy = sum(values)
    sxx = sum(i * i for i in range(n))
    sxy = sum(i * values[i] for i in range(n))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom


@dataclass(frozen=True)
class TrendInfo:
    regime: str
    ema_fast: float | None
    ema_slow: float | None
    slope: float
    last_close: float
    channel_high: float
    channel_low: float
    channel_mid: float
    channel_width_pct: float  # (high - low) / mid
    position_in_channel: float  # 0..1


def analyze_trend(
    bars: list[Bar],
    ema_fast_period: int = 20,
    ema_slow_period: int = 50,
    channel_window: int = 20,
    bull_bear_threshold: float = 0.005,  # 0.5% diff entre EMAs
    slope_threshold: float = 0.0008,     # ~0.08% por bar
) -> TrendInfo:
    closes = [b.close for b in bars]
    last_close = closes[-1] if closes else 0.0

    ema_fast = _ema(closes, ema_fast_period)
    ema_slow = _ema(closes, ema_slow_period)
    slope_window = closes[-min(channel_window, len(closes)):] if closes else []
    slope = _linear_slope(slope_window)

    # Donchian channel
    window_bars = bars[-channel_window:] if bars else []
    if window_bars:
        ch_high = max(b.high for b in window_bars)
        ch_low = min(b.low for b in window_bars)
    else:
        ch_high = last_close
        ch_low = last_close
    ch_mid = (ch_high + ch_low) / 2 if (ch_high + ch_low) > 0 else last_close
    width = ch_high - ch_low
    width_pct = (width / ch_mid) if ch_mid > 0 else 0.0
    pos_in_ch = ((last_close - ch_low) / width) if width > 0 else 0.5
    pos_in_ch = max(0.0, min(1.0, pos_in_ch))

    regime = "RANGE"
    if ema_fast is not None and ema_slow is not None:
        if (
            ema_fast > ema_slow * (1 + bull_bear_threshold)
            and slope > slope_threshold
        ):
            regime = "BULL"
        elif (
            ema_fast < ema_slow * (1 - bull_bear_threshold)
            and slope < -slope_threshold
        ):
            regime = "BEAR"

    return TrendInfo(
        regime=regime,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        slope=slope,
        last_close=last_close,
        channel_high=ch_high,
        channel_low=ch_low,
        channel_mid=ch_mid,
        channel_width_pct=width_pct,
        position_in_channel=pos_in_ch,
    )
