"""Helpers numéricos compartidos por los edges y el exit engine.

Antes vivían como privados (`_mid`, `_ema`) dentro de `overreaction.py` y el
exit engine los importaba con un try/except frágil. Centralizarlos evita ese
acoplamiento a internals.
"""

from __future__ import annotations

from umbra.features.calculator import SnapshotInput


def mid(s: SnapshotInput) -> float | None:
    """Mid del book; si falta un lado, cae al último trade."""
    if s.best_bid is None or s.best_ask is None:
        return s.last_trade_price
    return (s.best_bid + s.best_ask) / 2.0


def ema(values: list[float], alpha: float) -> float:
    """EMA simple sobre una serie ya ordenada cronológicamente."""
    e = values[0]
    for v in values[1:]:
        e = alpha * v + (1 - alpha) * e
    return e
