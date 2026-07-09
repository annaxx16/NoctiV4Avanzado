"""Tests de la capa de régimen: features, k-means determinista y caracterización."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from umbra.research.regime import (
    cluster_regimes,
    extract_regime_features,
    kmeans,
)
from umbra.research.series import PricePoint
from umbra.research.synthetic import generate_series

BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _series(values: list[float]) -> list[PricePoint]:
    return [PricePoint(ts=BASE + timedelta(minutes=i), value=v) for i, v in enumerate(values)]


def test_features_need_minimum_window():
    pts = _series([0.5, 0.5])
    assert extract_regime_features(pts, pts[-1].ts, window=timedelta(minutes=10)) is None


def test_features_measure_volatility_and_drift():
    calm = _series([0.50, 0.501, 0.499, 0.500, 0.501])
    vol_pts = _series([0.50, 0.60, 0.45, 0.65, 0.40])
    fc = extract_regime_features(calm, calm[-1].ts, window=timedelta(minutes=10))
    fv = extract_regime_features(vol_pts, vol_pts[-1].ts, window=timedelta(minutes=10))
    assert fc is not None and fv is not None
    assert fv.volatility > fc.volatility

    trend = _series([0.50, 0.55, 0.60, 0.66, 0.73])
    ft = extract_regime_features(trend, trend[-1].ts, window=timedelta(minutes=10))
    assert ft is not None
    assert ft.drift > 0.4  # subió ~46%


def test_kmeans_is_deterministic_and_separates():
    # dos nubes bien separadas en 1D
    data = [[0.0], [0.1], [0.2], [10.0], [10.1], [10.2]]
    labels_a, _ = kmeans(data, k=2)
    labels_b, _ = kmeans(data, k=2)
    assert labels_a == labels_b  # determinista
    # los tres primeros en un cluster, los tres últimos en otro
    assert labels_a[0] == labels_a[1] == labels_a[2]
    assert labels_a[3] == labels_a[4] == labels_a[5]
    assert labels_a[0] != labels_a[3]


def test_kmeans_handles_k_ge_n():
    labels, centroids = kmeans([[1.0], [2.0]], k=5)
    assert len(labels) == 2
    assert len(centroids) == 2  # k recortado a n


def test_cluster_regimes_labels_synthetic_cascade():
    """Sobre la serie sintética, debe emerger un régimen de alta volatilidad
    (VOLATILE/CASCADE) distinto del CALM dominante."""
    pts, _truth = generate_series()
    window = timedelta(minutes=15)
    feats = [
        f
        for i in range(0, len(pts), 2)
        if (f := extract_regime_features(pts, pts[i].ts, window=window)) is not None
    ]
    labels, profiles = cluster_regimes(feats, k=4)

    assert len(labels) == len(feats)
    names = {p.name for p in profiles}
    # debe distinguir al menos un estado turbulento de uno tranquilo
    assert "CALM" in names
    assert names & {"VOLATILE", "CASCADE", "TRENDING"}

    # el cluster más volátil debe tener vol claramente mayor que el menos volátil
    vols = sorted(p.mean_volatility for p in profiles)
    assert vols[-1] > vols[0] * 2


def test_cluster_regimes_empty():
    labels, profiles = cluster_regimes([], k=4)
    assert labels == []
    assert profiles == []
