"""Cuánto miente el backtest. El entregable de la Fase 3.

`execution/paper.py` predice el slippage con un modelo de una línea: una base, más
un factor por el ratio nocional/liquidez, capado. La «liquidez» es `volume_24hr`,
un agregado de Gamma que no dice nada sobre cómo están repartidas las órdenes en
el libro. Cada número de rentabilidad del backtest descansa sobre eso.

`exec` camina el libro real, nivel a nivel, y devuelve lo que el dinero habría
pagado. Este módulo resta las dos cosas.

CÓMO SE LEE
-----------
`divergence = realized - expected`, en bps, y positivo significa **peor de lo que
creías**. Si la mediana de `overreaction` sale en +180bps, tu backtest se está
regalando 1.8 puntos de precio en cada entrada.

TRES TRAMPAS AL LEER EL REPORTE
-------------------------------
1. **Los rechazos cuentan en el slippage, y deben.** Cuando el libro real habría
   costado más que `max_slippage_bps`, exec rechaza pero **conserva la medición**.
   Excluirlos dejaría fuera precisamente los peores libros, y la media saldría
   preciosa. Están dentro. Lo que no cuentan es en el ratio de llenado.

2. **El ratio de llenado es la otra mitad.** Un slippage mediano de 20bps sobre un
   30% de la orden no es una buena ejecución: es una orden que no se llenó. Los dos
   números se leen juntos, y por eso van en la misma tabla.

3. **La cotización de exec es optimista** (ver la cabecera de `quote.ts`): no simula
   impacto de mercado, ni competencia por los mismos niveles, ni latencia. La
   divergencia real es **al menos** ésta. Si con esta ya no hay edge, no lo hay.

ESTO NO ES EL CAMINO DEL DINERO
-------------------------------
Aquí se agrega con `float`, no con `Decimal`. Una mediana de basis points es una
medida, no un saldo: nadie deriva un asiento contable de este módulo, y el sexto
decimal de un percentil no significa nada. La regla de `Decimal` sigue en pie
donde importa —`paper.py`, el bus, las columnas— y no aquí.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.db.models import SHADOW_MODE, Fill, Intent

# Los tramos de tamaño. El slippage crece con el nocional, y una media que mezcle
# una orden de $10 con una de $2.000 no describe a ninguna de las dos.
#
# Las etiquetas son ASCII a propósito. Son claves de un dict que acaba impreso en
# una consola de Windows, cuya cp1252 no conoce `≥` y aborta con UnicodeEncodeError
# en vez de enseñar el reporte.
SIZE_BUCKETS: tuple[tuple[str, Decimal, Decimal | None], ...] = (
    ("<$25", Decimal("0"), Decimal("25")),
    ("$25-100", Decimal("25"), Decimal("100")),
    ("$100-250", Decimal("100"), Decimal("250")),
    ("$250-1k", Decimal("250"), Decimal("1000")),
    (">=$1k", Decimal("1000"), None),
)

# Un fill que llenó algo. Los demás no entran en el ratio de llenado.
FILLED_STATUSES = frozenset({"FILLED", "PARTIAL"})


@dataclass(frozen=True)
class Sample:
    """Un intent y lo que le pasó. `realized_bps` es `None` si no hubo libro."""

    intent_id: str
    strategy: str
    size_usd: Decimal
    # `None` si el intent murió antes de que exec contestara (expiró en el outbox).
    status: str | None
    expected_bps: Decimal | None
    realized_bps: Decimal | None
    # Lo que se habría llenado. Cero en un rechazo.
    notional_usd: Decimal

    @property
    def measurable(self) -> bool:
        """Hay las dos mitades de la resta."""
        return self.expected_bps is not None and self.realized_bps is not None

    @property
    def fill_ratio(self) -> float | None:
        if self.status not in FILLED_STATUSES or self.size_usd <= 0:
            return None
        return float(self.notional_usd / self.size_usd)


@dataclass(frozen=True)
class Stats:
    """La divergencia de un grupo. Todo en bps salvo `fill_ratio_*`."""

    n: int = 0
    expected_mean: float = 0.0
    realized_mean: float = 0.0
    divergence_mean: float = 0.0
    divergence_p50: float = 0.0
    divergence_p90: float = 0.0
    realized_p50: float = 0.0
    realized_p90: float = 0.0
    # Sobre los FILLED/PARTIAL del grupo. `None` si no hubo ninguno.
    n_filled: int = 0
    fill_ratio_mean: float | None = None

    @property
    def empty(self) -> bool:
        return self.n == 0


@dataclass(frozen=True)
class Report:
    since: datetime
    until: datetime
    # Cuántos intents se pidieron, midiesen o no.
    n_intents: int = 0
    # Cuántos tienen las dos mitades de la resta.
    n_measurable: int = 0
    overall: Stats = field(default_factory=Stats)
    by_strategy: dict[str, Stats] = field(default_factory=dict)
    by_size: dict[str, Stats] = field(default_factory=dict)
    # `FILLED`, `PARTIAL`, `REJECTED`, `EXPIRED`, `ERROR`, y `None` → «sin respuesta».
    status_counts: dict[str, int] = field(default_factory=dict)


def _percentile(sorted_values: list[float], q: float) -> float:
    """Percentil por interpolación lineal. `q` en [0, 1]."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    weight = pos - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(samples: list[Sample]) -> Stats:
    """Agrega un grupo. Los no medibles no entran; los rechazados sí.

    `fill_ratio_mean` se calcula sobre otro subconjunto —los que llenaron algo—,
    y por eso lleva su propio `n_filled`. Mezclarlos daría un ratio de llenado
    hundido por los rechazos, que por definición no llenaron nada.
    """
    measurable = [s for s in samples if s.measurable]
    ratios = [r for s in samples if (r := s.fill_ratio) is not None]

    if not measurable:
        return Stats(
            n=0,
            n_filled=len(ratios),
            fill_ratio_mean=_mean(ratios) if ratios else None,
        )

    expected = [float(s.expected_bps) for s in measurable]  # type: ignore[arg-type]
    realized = [float(s.realized_bps) for s in measurable]  # type: ignore[arg-type]
    divergence = [r - e for e, r in zip(expected, realized, strict=True)]

    div_sorted = sorted(divergence)
    real_sorted = sorted(realized)

    return Stats(
        n=len(measurable),
        expected_mean=_mean(expected),
        realized_mean=_mean(realized),
        divergence_mean=_mean(divergence),
        divergence_p50=_percentile(div_sorted, 0.50),
        divergence_p90=_percentile(div_sorted, 0.90),
        realized_p50=_percentile(real_sorted, 0.50),
        realized_p90=_percentile(real_sorted, 0.90),
        n_filled=len(ratios),
        fill_ratio_mean=_mean(ratios) if ratios else None,
    )


def bucket_of(size_usd: Decimal) -> str:
    """El tramo de tamaño al que pertenece un nocional."""
    for name, low, high in SIZE_BUCKETS:
        if size_usd >= low and (high is None or size_usd < high):
            return name
    return SIZE_BUCKETS[-1][0]  # pragma: no cover — el último tramo no tiene techo


def build_report(samples: list[Sample], since: datetime, until: datetime) -> Report:
    """Agrega la muestra entera. Función pura: entra una lista, sale el reporte."""
    by_strategy: dict[str, list[Sample]] = {}
    by_size: dict[str, list[Sample]] = {}
    status_counts: dict[str, int] = {}

    for s in samples:
        by_strategy.setdefault(s.strategy, []).append(s)
        by_size.setdefault(bucket_of(s.size_usd), []).append(s)
        key = s.status or "SIN_RESPUESTA"
        status_counts[key] = status_counts.get(key, 0) + 1

    return Report(
        since=since,
        until=until,
        n_intents=len(samples),
        n_measurable=sum(1 for s in samples if s.measurable),
        overall=summarize(samples),
        by_strategy={k: summarize(v) for k, v in sorted(by_strategy.items())},
        # En el orden de los tramos, no en el alfabético.
        by_size={
            name: summarize(by_size[name]) for name, _, _ in SIZE_BUCKETS if name in by_size
        },
        status_counts=dict(sorted(status_counts.items())),
    )


# ---------------------------------------------------------------------------
# El cargador. Lo único que toca la base.
# ---------------------------------------------------------------------------


async def load_samples(
    session: AsyncSession, since: datetime, until: datetime | None = None
) -> list[Sample]:
    """Todo lo que brain pidió en shadow, con lo que exec contestó o sin ello.

    `LEFT JOIN`, no `JOIN`. Un intent que expiró en el outbox, o al que exec nunca
    contestó, no tiene fila en `fills` — y es exactamente el caso que la tabla
    `intents` existe para no perder. Con un `JOIN` interno, el silencio se
    confundiría con «no hubo señal».
    """
    until = until or datetime.now(UTC)
    stmt = (
        select(
            Intent.intent_id,
            Intent.strategy,
            Intent.size_usd,
            Intent.status,
            Intent.expected_slippage_bps,
            Fill.slippage_bps,
            Fill.notional_usd,
        )
        .join(Fill, Fill.intent_id == Intent.intent_id, isouter=True)
        .where(Intent.mode == SHADOW_MODE, Intent.ts >= since, Intent.ts < until)
        .order_by(Intent.ts)
    )
    rows = (await session.execute(stmt)).all()
    return [
        Sample(
            intent_id=row.intent_id,
            strategy=row.strategy,
            size_usd=row.size_usd,
            status=row.status,
            expected_bps=row.expected_slippage_bps,
            realized_bps=row.slippage_bps,
            notional_usd=row.notional_usd or Decimal("0"),
        )
        for row in rows
    ]


async def shadow_report(
    session: AsyncSession, days: int = 14, until: datetime | None = None
) -> Report:
    """El reporte de los últimos `days` días. Dos semanas es el criterio de la Fase 3."""
    until = until or datetime.now(UTC)
    since = until - timedelta(days=days)
    return build_report(await load_samples(session, since, until), since, until)
