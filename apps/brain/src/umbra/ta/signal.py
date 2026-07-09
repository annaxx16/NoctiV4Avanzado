"""TA context for entries and exits.

The overreaction edge is mean-reversion by design. TA should usually size down
or lower confidence when momentum is against us, not veto every contrarian
setup. Hard TA vetoes stay configurable for live mode or stricter experiments.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from umbra.config import settings
from umbra.ta.levels import LevelsView, classify_levels
from umbra.ta.ohlc import Bar, read_bars
from umbra.ta.trend import TrendInfo, analyze_trend


@dataclass(frozen=True)
class EntryTaVerdict:
    confidence: float
    reject: bool
    reason: str
    trend: TrendInfo | None
    levels: LevelsView | None


@dataclass(frozen=True)
class ExitTaVerdict:
    close: bool
    reason: str | None
    trend: TrendInfo | None
    levels: LevelsView | None


_MIN_BARS_FOR_TA = 10


async def _load_context(
    session: AsyncSession, market_id: str
) -> tuple[list[Bar], TrendInfo | None, LevelsView | None]:
    bars = await read_bars(session, market_id, "5m", n=settings.ohlc_lookback_bars)
    if len(bars) < _MIN_BARS_FOR_TA:
        return bars, None, None
    trend = analyze_trend(
        bars,
        ema_fast_period=settings.ta_ema_fast,
        ema_slow_period=settings.ta_ema_slow,
    )
    levels = classify_levels(
        bars,
        window=3,
        tolerance=0.015,
        min_touches=settings.ta_sr_min_touches,
    )
    return bars, trend, levels


async def evaluate_entry(
    session: AsyncSession,
    market_id: str,
    side: str,
) -> EntryTaVerdict:
    bars, trend, levels = await _load_context(session, market_id)

    if trend is None or levels is None:
        return EntryTaVerdict(
            confidence=0.5,
            reject=False,
            reason="ta_insufficient_bars",
            trend=trend,
            levels=levels,
        )

    confidence = 0.5
    reason = "ta_neutral"

    if side == "BUY_NO":
        if trend.regime == "BULL":
            confidence -= 0.3
            reason = "ta_against_bull_trend"
            if trend.position_in_channel >= 0.85 and trend.slope > 0:
                confidence -= 0.2
                reason = "ta_penalty_bull_breakout_buyno"
                if settings.ta_hard_reject_enabled:
                    return EntryTaVerdict(
                        confidence=0.0,
                        reject=True,
                        reason="ta_reject_bull_breakout_buyno",
                        trend=trend,
                        levels=levels,
                    )
        elif trend.regime == "BEAR":
            confidence += 0.2
            reason = "ta_aligned_bear"
        if (
            levels.nearest_resistance is not None
            and abs(levels.last_close - levels.nearest_resistance.price) < 0.02
        ):
            confidence += 0.1
            reason += "+resistance_nearby"

    elif side == "BUY_YES":
        if trend.regime == "BEAR":
            confidence -= 0.3
            reason = "ta_against_bear_trend"
            if trend.position_in_channel <= 0.15 and trend.slope < 0:
                confidence -= 0.2
                reason = "ta_penalty_bear_breakdown_buyyes"
                if settings.ta_hard_reject_enabled:
                    return EntryTaVerdict(
                        confidence=0.0,
                        reject=True,
                        reason="ta_reject_bear_breakdown_buyyes",
                        trend=trend,
                        levels=levels,
                    )
        elif trend.regime == "BULL":
            confidence += 0.2
            reason = "ta_aligned_bull"
        if (
            levels.nearest_support is not None
            and abs(levels.last_close - levels.nearest_support.price) < 0.02
        ):
            confidence += 0.1
            reason += "+support_nearby"

    confidence = max(0.0, min(1.0, confidence))
    return EntryTaVerdict(
        confidence=confidence,
        reject=False,
        reason=reason,
        trend=trend,
        levels=levels,
    )


async def evaluate_exit_ta(
    session: AsyncSession,
    market_id: str,
    side: str,
) -> ExitTaVerdict:
    bars, trend, levels = await _load_context(session, market_id)
    if trend is None or levels is None:
        return ExitTaVerdict(close=False, reason=None, trend=trend, levels=levels)

    if side == "BUY_NO" and trend.regime == "BULL":
        if trend.position_in_channel >= 0.85 and trend.slope > 0:
            return ExitTaVerdict(
                close=True,
                reason="t11_ta_trend_against_bull",
                trend=trend,
                levels=levels,
            )
    if side == "BUY_YES" and trend.regime == "BEAR":
        if trend.position_in_channel <= 0.15 and trend.slope < 0:
            return ExitTaVerdict(
                close=True,
                reason="t11_ta_trend_against_bear",
                trend=trend,
                levels=levels,
            )

    last_close = trend.last_close
    if side == "BUY_YES" and levels.nearest_support is not None:
        if last_close < levels.nearest_support.price * 0.99:
            return ExitTaVerdict(
                close=True,
                reason="t12_ta_support_broken",
                trend=trend,
                levels=levels,
            )
    if side == "BUY_NO" and levels.nearest_resistance is not None:
        if last_close > levels.nearest_resistance.price * 1.01:
            return ExitTaVerdict(
                close=True,
                reason="t12_ta_resistance_broken",
                trend=trend,
                levels=levels,
            )

    return ExitTaVerdict(close=False, reason=None, trend=trend, levels=levels)
