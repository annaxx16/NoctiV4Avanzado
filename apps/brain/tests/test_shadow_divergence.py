"""El reporte de divergencia, en su parte pura.

Todo esto entra por una lista de `Sample` y sale por un `Report`. `load_samples`
toca la base y se prueba en `tests/test_bus_fills_db.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from umbra.analytics.shadow_divergence import (
    Sample,
    _percentile,
    bucket_of,
    build_report,
    summarize,
)

_SINCE = datetime(2026, 7, 1, tzinfo=UTC)
_UNTIL = datetime(2026, 7, 15, tzinfo=UTC)


def _sample(
    *,
    strategy: str = "overreaction",
    size: str = "100",
    status: str | None = "FILLED",
    expected: str | None = "30",
    realized: str | None = "50",
    notional: str | None = None,
    intent_id: str = "i",
) -> Sample:
    """Por defecto: llenado entero, 30bps predichos, 50 reales. Diverge +20."""
    return Sample(
        intent_id=intent_id,
        strategy=strategy,
        size_usd=Decimal(size),
        status=status,
        expected_bps=None if expected is None else Decimal(expected),
        realized_bps=None if realized is None else Decimal(realized),
        notional_usd=Decimal(notional if notional is not None else size),
    )


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def test_percentil_de_una_lista_vacia_o_unitaria():
    assert _percentile([], 0.5) == 0.0
    assert _percentile([7.0], 0.9) == 7.0


def test_percentil_interpola():
    valores = [0.0, 10.0, 20.0, 30.0]
    assert _percentile(valores, 0.0) == 0.0
    assert _percentile(valores, 1.0) == 30.0
    assert _percentile(valores, 0.5) == 15.0


# ---------------------------------------------------------------------------
# Tramos de tamaño
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "tramo"),
    [
        ("0", "<$25"),
        ("24.99", "<$25"),
        ("25", "$25-100"),
        ("99.99", "$25-100"),
        ("100", "$100-250"),
        ("250", "$250-1k"),
        ("999.99", "$250-1k"),
        ("1000", ">=$1k"),
        ("50000", ">=$1k"),
    ],
)
def test_bucket_of(size, tramo):
    assert bucket_of(Decimal(size)) == tramo


# ---------------------------------------------------------------------------
# La resta
# ---------------------------------------------------------------------------


def test_divergencia_positiva_es_peor_de_lo_previsto():
    stats = summarize([_sample(expected="30", realized="50")])
    assert stats.n == 1
    assert stats.expected_mean == 30.0
    assert stats.realized_mean == 50.0
    assert stats.divergence_mean == 20.0


def test_divergencia_negativa_cuando_el_libro_fue_mejor():
    stats = summarize([_sample(expected="50", realized="20")])
    assert stats.divergence_mean == -30.0


def test_un_slippage_realizado_negativo_es_un_numero_real():
    """Vendiendo por encima del mid, o comprando por debajo. Positivo = adverso."""
    stats = summarize([_sample(expected="10", realized="-40")])
    assert stats.realized_mean == -40.0
    assert stats.divergence_mean == -50.0


def test_los_no_medibles_no_entran_en_la_resta():
    """Un EXPIRED no tiene libro contra el que medirse. No arrastra la media a cero."""
    muestras = [
        _sample(expected="30", realized="50"),
        _sample(status="EXPIRED", realized=None, notional="0"),
        _sample(status="ERROR", expected=None, realized=None, notional="0"),
    ]
    stats = summarize(muestras)
    assert stats.n == 1
    assert stats.divergence_mean == 20.0


def test_los_rechazados_si_cuentan_en_el_slippage():
    """Son los peores libros. Excluirlos dejaría una media preciosa y falsa."""
    muestras = [
        _sample(expected="30", realized="40"),
        _sample(status="REJECTED", expected="30", realized="450", notional="0"),
    ]
    stats = summarize(muestras)
    assert stats.n == 2
    assert stats.realized_mean == 245.0


def test_los_rechazados_no_cuentan_en_el_ratio_de_llenado():
    """Un rechazo no llenó nada; meterlo hundiría el ratio de los que sí llenaron."""
    muestras = [
        _sample(status="FILLED", size="100", notional="100"),
        _sample(status="REJECTED", size="100", notional="0"),
    ]
    stats = summarize(muestras)
    assert stats.n_filled == 1
    assert stats.fill_ratio_mean == 1.0


def test_un_partial_baja_el_ratio_de_llenado_sin_tocar_el_slippage():
    """Un slippage bonito sobre el 30% de la orden no es una buena ejecución."""
    muestras = [
        _sample(status="PARTIAL", size="100", notional="30", expected="30", realized="35"),
    ]
    stats = summarize(muestras)
    assert stats.fill_ratio_mean == pytest.approx(0.30)
    assert stats.divergence_mean == 5.0


def test_sin_ninguna_medicion_el_grupo_queda_vacio_pero_informa_del_llenado():
    stats = summarize([_sample(status="FILLED", expected=None, realized=None)])
    assert stats.empty
    assert stats.n == 0
    assert stats.n_filled == 1
    assert stats.fill_ratio_mean == 1.0


def test_sin_llenados_el_ratio_es_none_y_no_cero():
    """Cero llenado y «no hubo ninguno que pudiera llenarse» no son lo mismo."""
    stats = summarize([_sample(status="REJECTED", notional="0")])
    assert stats.fill_ratio_mean is None


def test_percentiles_de_la_divergencia():
    muestras = [
        _sample(expected="0", realized=str(v), intent_id=f"i{v}") for v in (0, 10, 20, 30)
    ]
    stats = summarize(muestras)
    assert stats.divergence_p50 == pytest.approx(15.0)
    assert stats.divergence_p90 == pytest.approx(27.0)


# ---------------------------------------------------------------------------
# El reporte completo
# ---------------------------------------------------------------------------


def test_build_report_agrupa_por_estrategia_y_por_tamano():
    muestras = [
        _sample(strategy="overreaction", size="50", expected="20", realized="40"),
        _sample(strategy="overreaction", size="500", expected="20", realized="120"),
        _sample(strategy="momentum", size="50", expected="20", realized="25"),
    ]
    report = build_report(muestras, _SINCE, _UNTIL)

    assert report.n_intents == 3
    assert report.n_measurable == 3

    assert set(report.by_strategy) == {"overreaction", "momentum"}
    assert report.by_strategy["overreaction"].divergence_mean == 60.0
    assert report.by_strategy["momentum"].divergence_mean == 5.0

    assert report.by_size["$25-100"].n == 2
    assert report.by_size["$250-1k"].divergence_mean == 100.0


def test_los_tramos_salen_en_orden_de_tamano_no_alfabetico():
    muestras = [
        _sample(size="1500", intent_id="a"),
        _sample(size="10", intent_id="b"),
        _sample(size="150", intent_id="c"),
    ]
    report = build_report(muestras, _SINCE, _UNTIL)
    assert list(report.by_size) == ["<$25", "$100-250", ">=$1k"]


def test_los_intents_sin_respuesta_se_cuentan_aparte():
    """Un intent que expiró en el outbox no deja fila en `fills`. No se pierde."""
    muestras = [
        _sample(status=None, expected="30", realized=None, notional="0"),
        _sample(status="FILLED"),
    ]
    report = build_report(muestras, _SINCE, _UNTIL)
    assert report.status_counts == {"FILLED": 1, "SIN_RESPUESTA": 1}
    assert report.n_intents == 2
    assert report.n_measurable == 1


def test_un_reporte_vacio_no_revienta():
    report = build_report([], _SINCE, _UNTIL)
    assert report.n_intents == 0
    assert report.overall.empty
    assert report.by_strategy == {}
    assert report.status_counts == {}
