"""Detección exploratoria de régimen (Fase 0, observacional).

Etiqueta el *estado* del mercado en cada instante a partir de features derivables
solo del precio (volatilidad, drift, drawdown), de modo que la capa sirve igual
para Polymarket y para activos continuos. El clustering es k-means determinista
en stdlib puro: sin RNG, init farthest-point → resultados reproducibles y
testeables offline.

ANTI-LOOKAHEAD: `extract_regime_features` solo usa puntos con ts ≤ as_of, igual
que el feature calculator de producción.

Esto NO decide trades. Productivizarlo (HMM/GMM, condicionar pesos del composite)
es Fase 2 y está detrás del gate de validación de OverreactionV1.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from umbra.research.series import PricePoint, returns

# Nombres de los ejes del vector de clustering (orden fijo).
FEATURE_NAMES = ("volatility", "abs_drift", "drawdown_depth")


@dataclass(frozen=True)
class RegimeFeatures:
    """Vector de estado en `as_of`, calculado sobre una ventana trailing."""

    as_of: datetime
    n: int  # puntos usados en la ventana
    volatility: float  # pstdev de retornos relativos en la ventana
    drift: float  # cambio relativo neto pico→final (con signo)
    drawdown_depth: float  # caída desde el máximo de la ventana, fracción positiva

    def vector(self) -> list[float]:
        """Ejes usados para clustering (drift en valor absoluto: el régimen es
        simétrico entre subir y bajar fuerte; el signo se reporta aparte)."""
        return [self.volatility, abs(self.drift), self.drawdown_depth]


def extract_regime_features(
    points: list[PricePoint], as_of: datetime, *, window: timedelta
) -> RegimeFeatures | None:
    """Features de régimen en `as_of` usando solo puntos en (as_of - window, as_of].

    Devuelve None si no hay al menos 3 puntos en la ventana (volatilidad inestable).
    """
    start = as_of - window
    win = [p for p in points if start < p.ts <= as_of]
    if len(win) < 3:
        return None

    rets = returns(win)
    vol = statistics.pstdev(rets) if len(rets) >= 2 else 0.0

    first_v = win[0].value
    last_v = win[-1].value
    drift = (last_v - first_v) / first_v if first_v > 0 else 0.0

    peak = max(p.value for p in win)
    dd = (peak - last_v) / peak if peak > 0 else 0.0

    return RegimeFeatures(
        as_of=as_of,
        n=len(win),
        volatility=vol,
        drift=drift,
        drawdown_depth=dd,
    )


def _standardize(rows: list[list[float]]) -> list[list[float]]:
    """z-score por columna. Columna de varianza 0 → queda en ceros."""
    if not rows:
        return []
    ncol = len(rows[0])
    cols = [[r[j] for r in rows] for j in range(ncol)]
    means = [statistics.fmean(c) for c in cols]
    stds = [statistics.pstdev(c) for c in cols]
    return [
        [
            (r[j] - means[j]) / stds[j] if stds[j] > 1e-12 else 0.0
            for j in range(ncol)
        ]
        for r in rows
    ]


def _dist2(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=False))


def _init_centroids(data: list[list[float]], k: int) -> list[list[float]]:
    """Farthest-point determinista: arranca en el punto 0 y elige iterativamente
    el más lejano a los centroides ya escogidos. Sin RNG → reproducible."""
    centroids = [list(data[0])]
    while len(centroids) < k:
        best_i, best_d = 0, -1.0
        for i, row in enumerate(data):
            d = min(_dist2(row, c) for c in centroids)
            if d > best_d:
                best_d, best_i = d, i
        centroids.append(list(data[best_i]))
    return centroids


def kmeans(
    data: list[list[float]], k: int, *, max_iter: int = 100
) -> tuple[list[int], list[list[float]]]:
    """k-means de Lloyd, determinista. Devuelve (labels, centroides).

    `data` ya debe venir estandarizado. Si k ≥ n, cada punto es su propio cluster.
    Clusters que quedan vacíos conservan su centroide previo.
    """
    n = len(data)
    if n == 0:
        return [], []
    k = min(k, n)
    centroids = _init_centroids(data, k)
    labels = [0] * n

    for _ in range(max_iter):
        changed = False
        for i, row in enumerate(data):
            dists = [_dist2(row, c) for c in centroids]
            best = min(range(k), key=lambda j: dists[j])
            if best != labels[i]:
                labels[i] = best
                changed = True

        for j in range(k):
            members = [data[i] for i in range(n) if labels[i] == j]
            if members:
                centroids[j] = [
                    statistics.fmean([m[d] for m in members])
                    for d in range(len(members[0]))
                ]
        if not changed:
            break

    return labels, centroids


@dataclass(frozen=True)
class RegimeProfile:
    """Caracterización de un cluster en unidades originales (no estandarizadas)."""

    cluster_id: int
    name: str
    size: int
    mean_volatility: float
    mean_abs_drift: float
    mean_drawdown_depth: float


def _name_regime(vol: float, abs_drift: float, dd: float, *, vol_hi: float) -> str:
    """Heurística de etiqueta. `vol_hi` es la mediana de volatilidad del dataset,
    así el nombre es relativo al propio activo (un mid y un precio no comparten
    escala absoluta de volatilidad)."""
    high_vol = vol >= vol_hi
    deep_dd = dd >= 0.10
    strong_drift = abs_drift >= 0.05

    if high_vol and deep_dd:
        return "CASCADE"
    if high_vol:
        return "VOLATILE"
    if strong_drift:
        return "TRENDING"
    return "CALM"


def cluster_regimes(
    features: list[RegimeFeatures], k: int = 4
) -> tuple[list[int], list[RegimeProfile]]:
    """Agrupa los vectores de régimen en k clusters y los caracteriza.

    Devuelve (labels alineadas con `features`, perfiles ordenados por cluster_id).
    """
    if not features:
        return [], []

    rows = [f.vector() for f in features]
    labels, _ = kmeans(_standardize(rows), k)
    actual_k = max(labels) + 1

    vols = [f.volatility for f in features]
    vol_median = statistics.median(vols)

    profiles: list[RegimeProfile] = []
    for cid in range(actual_k):
        members = [features[i] for i in range(len(features)) if labels[i] == cid]
        if not members:
            continue
        mv = statistics.fmean([m.volatility for m in members])
        md = statistics.fmean([abs(m.drift) for m in members])
        mdd = statistics.fmean([m.drawdown_depth for m in members])
        profiles.append(
            RegimeProfile(
                cluster_id=cid,
                name=_name_regime(mv, md, mdd, vol_hi=vol_median),
                size=len(members),
                mean_volatility=mv,
                mean_abs_drift=md,
                mean_drawdown_depth=mdd,
            )
        )

    return labels, profiles
