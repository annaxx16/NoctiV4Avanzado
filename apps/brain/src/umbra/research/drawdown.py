"""Análisis de drawdown: *cómo y cuándo quiebra* una serie.

Puro, stdlib only, agnóstico al dominio (opera sobre `PricePoint`). Descompone
la serie en episodios pico→valle→recuperación y resume su distribución. Esta es
la materia prima para etiquetar el régimen de cola (PRE-CRASH / CASCADE) y para
medir el riesgo realizado del histórico.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime

from umbra.research.series import PricePoint


@dataclass(frozen=True)
class DrawdownEpisode:
    """Un episodio de caída desde un pico hasta su valle, con recuperación opcional."""

    peak_ts: datetime
    peak_value: float
    trough_ts: datetime
    trough_value: float
    recovery_ts: datetime | None  # None = nunca recuperó dentro de la serie
    depth: float  # fracción positiva: (peak - trough) / peak
    time_to_trough_s: float
    time_to_recovery_s: float | None  # peak → recovery; None si no recuperó

    @property
    def recovered(self) -> bool:
        return self.recovery_ts is not None


def drawdown_series(points: list[PricePoint]) -> list[float]:
    """Drawdown corriente en cada punto: (pico_previo - valor) / pico_previo.

    Fracción positiva (0 = en máximos, 0.30 = 30% por debajo del pico). Asume
    valores positivos (precios y mids lo son).
    """
    out: list[float] = []
    peak = float("-inf")
    for p in points:
        peak = max(peak, p.value)
        out.append((peak - p.value) / peak if peak > 0 else 0.0)
    return out


def find_drawdown_episodes(
    points: list[PricePoint], *, min_depth: float = 0.05
) -> list[DrawdownEpisode]:
    """Descompone la serie en episodios de drawdown ≥ `min_depth`.

    Un episodio nace cuando el valor cae bajo el pico vigente, su valle es el
    mínimo hasta que un nuevo punto recupera/supera ese pico, y se cierra ahí.
    Si la serie termina aún por debajo del pico, el episodio queda sin recuperar
    (`recovery_ts=None`).
    """
    if len(points) < 2:
        return []

    episodes: list[DrawdownEpisode] = []
    peak = points[0].value
    peak_ts = points[0].ts
    in_dd = False
    trough_val = peak
    trough_ts = peak_ts

    def _emit(recovery_ts: datetime | None) -> None:
        depth = (peak - trough_val) / peak if peak > 0 else 0.0
        if depth < min_depth:
            return
        episodes.append(
            DrawdownEpisode(
                peak_ts=peak_ts,
                peak_value=peak,
                trough_ts=trough_ts,
                trough_value=trough_val,
                recovery_ts=recovery_ts,
                depth=depth,
                time_to_trough_s=(trough_ts - peak_ts).total_seconds(),
                time_to_recovery_s=(
                    (recovery_ts - peak_ts).total_seconds()
                    if recovery_ts is not None
                    else None
                ),
            )
        )

    for p in points[1:]:
        if p.value >= peak:
            if in_dd:
                _emit(recovery_ts=p.ts)
                in_dd = False
            peak = p.value
            peak_ts = p.ts
        else:
            if not in_dd:
                in_dd = True
                trough_val = p.value
                trough_ts = p.ts
            elif p.value < trough_val:
                trough_val = p.value
                trough_ts = p.ts

    if in_dd:
        _emit(recovery_ts=None)

    return episodes


@dataclass(frozen=True)
class DrawdownStats:
    n_episodes: int
    max_drawdown: float
    mean_depth: float
    worst_episode: DrawdownEpisode | None
    mean_time_to_recovery_s: float | None  # solo sobre episodios recuperados
    n_unrecovered: int
    fraction_underwater: float  # % de puntos por debajo de su pico previo


def summarize_drawdown(
    points: list[PricePoint], *, min_depth: float = 0.05
) -> DrawdownStats:
    """Resumen del perfil de drawdown de la serie."""
    episodes = find_drawdown_episodes(points, min_depth=min_depth)
    dd = drawdown_series(points)

    recovered_times = [
        e.time_to_recovery_s for e in episodes if e.time_to_recovery_s is not None
    ]
    worst = max(episodes, key=lambda e: e.depth) if episodes else None

    return DrawdownStats(
        n_episodes=len(episodes),
        max_drawdown=max(dd) if dd else 0.0,
        mean_depth=statistics.fmean([e.depth for e in episodes]) if episodes else 0.0,
        worst_episode=worst,
        mean_time_to_recovery_s=(
            statistics.fmean(recovered_times) if recovered_times else None
        ),
        n_unrecovered=sum(1 for e in episodes if not e.recovered),
        fraction_underwater=(
            sum(1 for d in dd if d > 1e-9) / len(dd) if dd else 0.0
        ),
    )
