"""Reporte de divergencia de la Fase 3: cuánto miente el backtest.

Compara el slippage que `execution/paper.py` predijo contra el que `exec` midió
caminando el libro real, por estrategia y por tramo de tamaño.

Positivo = peor de lo que creías.

Uso:
    python scripts/shadow_report.py [--days 14]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from umbra.analytics.shadow_divergence import Report, Stats, shadow_report  # noqa: E402
from umbra.db.session import get_sessionmaker  # noqa: E402

# Sin medición no se imprime un cero: se dice que no la hay.
_NA = "-"

_COLS = ("grupo", "n", "esperado", "real", "diverg.", "p50", "p90", "llenado")
_WIDTHS = (12, 6, 10, 10, 10, 10, 10, 9)


def _row(cells: tuple[str, ...]) -> str:
    return "  ".join(c.rjust(w) for c, w in zip(cells, _WIDTHS, strict=True))


def _fmt_stats(name: str, s: Stats) -> str:
    if s.empty:
        llenado = _NA if s.fill_ratio_mean is None else f"{s.fill_ratio_mean:.0%}"
        return _row((name, "0", _NA, _NA, _NA, _NA, _NA, llenado))
    return _row(
        (
            name,
            str(s.n),
            f"{s.expected_mean:.1f}",
            f"{s.realized_mean:.1f}",
            f"{s.divergence_mean:+.1f}",
            f"{s.divergence_p50:+.1f}",
            f"{s.divergence_p90:+.1f}",
            _NA if s.fill_ratio_mean is None else f"{s.fill_ratio_mean:.0%}",
        )
    )


def render(report: Report) -> str:
    out: list[str] = []
    out.append("=" * 88)
    out.append("DIVERGENCIA DE SLIPPAGE — predicho vs. libro real (bps, + = peor de lo previsto)")
    out.append(f"ventana: {report.since:%Y-%m-%d %H:%M} -> {report.until:%Y-%m-%d %H:%M} UTC")
    out.append("=" * 88)

    out.append(f"\nintents emitidos: {report.n_intents}   medibles: {report.n_measurable}")
    if report.status_counts:
        estados = "  ".join(f"{k}={v}" for k, v in report.status_counts.items())
        out.append(f"estados: {estados}")

    if report.n_measurable == 0:
        out.append(
            "\nNo hay una sola medición todavía. Sin `realized` no hay resta, y sin resta\n"
            "este reporte no dice nada. Comprueba que exec consume `nocti:intents`."
        )
        return "\n".join(out)

    out.append("\n" + _row(_COLS))
    out.append("-" * 88)
    out.append(_fmt_stats("TODO", report.overall))

    out.append("\npor estrategia")
    out.append("-" * 88)
    for name, stats in report.by_strategy.items():
        out.append(_fmt_stats(name, stats))

    out.append("\npor tamaño")
    out.append("-" * 88)
    for name, stats in report.by_size.items():
        out.append(_fmt_stats(name, stats))

    out.append(
        "\nnota: los REJECTED por exceso de slippage cuentan en las columnas de bps —son\n"
        "los peores libros, y excluirlos halagaría la media— pero no en 'llenado'.\n"
        "La cotización de exec no simula impacto ni latencia: la divergencia real es\n"
        "AL MENOS ésta."
    )
    return "\n".join(out)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="ventana en días (default: 14)")
    args = parser.parse_args()

    sm = get_sessionmaker()
    async with sm() as session:
        report = await shadow_report(session, days=args.days)
    print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
