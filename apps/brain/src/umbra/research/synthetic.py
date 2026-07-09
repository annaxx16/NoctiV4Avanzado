"""Generador sintético de series con regímenes conocidos.

Permite correr y testear la capa de régimen SIN datos reales: encadena tramos
CALM / TRENDING / CASCADE con parámetros controlados, de modo que el clustering
tiene una respuesta correcta verificable. Determinista vía `random.Random(seed)`.

No es un modelo de mercado: es un banco de pruebas con ground-truth.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from umbra.research.series import PricePoint


@dataclass(frozen=True)
class Segment:
    name: str  # etiqueta ground-truth (CALM/TRENDING/CASCADE/...)
    n: int  # número de puntos
    drift_per_step: float  # deriva relativa media por paso
    vol: float  # desviación relativa del ruido por paso


DEFAULT_SCENARIO = (
    Segment("CALM", n=60, drift_per_step=0.0002, vol=0.003),
    Segment("TRENDING", n=40, drift_per_step=0.006, vol=0.004),
    Segment("CALM", n=40, drift_per_step=0.0, vol=0.003),
    Segment("CASCADE", n=20, drift_per_step=-0.02, vol=0.02),
    Segment("CALM", n=50, drift_per_step=0.001, vol=0.003),
)


def generate_series(
    segments: tuple[Segment, ...] = DEFAULT_SCENARIO,
    *,
    start_value: float = 0.50,
    step: timedelta = timedelta(minutes=1),
    seed: int = 7,
    start_ts: datetime | None = None,
) -> tuple[list[PricePoint], list[str]]:
    """Genera (serie, etiquetas_ground_truth) alineadas punto a punto.

    El valor se mantiene en (0, 1) con un clamp suave para parecerse a un mid de
    Polymarket; subir `start_value` y quitar el clamp lo vuelve estilo precio.
    """
    rng = random.Random(seed)
    ts = start_ts or datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    value = start_value
    points: list[PricePoint] = []
    labels: list[str] = []

    for seg in segments:
        for _ in range(seg.n):
            shock = rng.gauss(seg.drift_per_step, seg.vol)
            value = max(0.01, min(0.99, value * (1.0 + shock)))
            points.append(PricePoint(ts=ts, value=value))
            labels.append(seg.name)
            ts += step

    return points, labels
