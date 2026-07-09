"""Capa de investigación (Fase 0 del plan de régimen v3).

OBSERVACIONAL, no toca el path de trading. Aquí vive el análisis exploratorio
sobre el histórico — drawdown y detección de régimen — que NO compromete capital
ni viola la disciplina del plan (no añade edges, no decide órdenes). Su único
objetivo es responder: *¿existen regímenes detectables y estables en los datos?*

La abstracción central es `PricePoint`: una serie genérica `(ts, value)` a la que
se reduce tanto un mid de Polymarket (∈ [0,1]) como el close de un activo continuo
(precio no acotado). Por eso el módulo es agnóstico al dominio: la misma capa de
régimen corre sobre prediction markets y sobre cripto/acciones.
"""

from __future__ import annotations

from umbra.research.drawdown import (
    DrawdownEpisode,
    DrawdownStats,
    drawdown_series,
    find_drawdown_episodes,
    summarize_drawdown,
)
from umbra.research.regime import (
    RegimeFeatures,
    RegimeProfile,
    cluster_regimes,
    extract_regime_features,
    kmeans,
)
from umbra.research.series import PricePoint, snapshots_to_series

__all__ = [
    "PricePoint",
    "snapshots_to_series",
    "DrawdownEpisode",
    "DrawdownStats",
    "drawdown_series",
    "find_drawdown_episodes",
    "summarize_drawdown",
    "RegimeFeatures",
    "RegimeProfile",
    "cluster_regimes",
    "extract_regime_features",
    "kmeans",
]
