"""Probability Engine — versión D4 es passthrough.

En D4 no calibramos: p_fair = fair_price del edge. En el futuro (post-D5) este
módulo aplicará calibración bayesiana sobre histórico OOS.
"""

from __future__ import annotations

from umbra.edges.overreaction import EdgeOutput


def compute_p_fair(edge: EdgeOutput) -> float:
    """Retorna probabilidad justa de que el contrato (lado YES) se resuelva 1.

    PASSTHROUGH (v1): p_fair = EMA del mid (lado YES). NO está calibrada —
    el sizer la usa como insumo de Kelly. La calibración bayesiana sobre
    outcomes resueltos es trabajo de la v2 (ver GAP-01 en RESTRUCTURE_PLAN).

    Clamp a (0.001, 0.999): una probabilidad en {0, 1} haría que Kelly trate
    la apuesta como certeza y dimensione sin tope racional.
    """
    return min(0.999, max(0.001, float(edge.fair_price)))
