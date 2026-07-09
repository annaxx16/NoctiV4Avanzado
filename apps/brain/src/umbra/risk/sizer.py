"""Position sizer: Kelly fraccional sobre contratos binarios (Polymarket).

Para un contrato YES con precio de mercado p_m y probabilidad justa p_f:
- Pago si gana: 1 USD por share
- Costo: p_m USD por share
- Probabilidad de ganar (según nuestra estimación): p_f
- Odds: b = (1 - p_m) / p_m  (cuánto ganamos por cada $ apostado)
- Kelly óptimo: f* = (p_f * b - (1 - p_f)) / b

Aplicamos κ < 1 (fractional Kelly) para reducir varianza y sobreestimación.
"""

from __future__ import annotations

from dataclasses import dataclass

from umbra.config import settings


@dataclass(frozen=True)
class SizingResult:
    f_star: float
    shares: float
    notional_usd: float


def _kelly_fraction(p_fair: float, price: float) -> float:
    if price <= 0 or price >= 1:
        return 0.0
    b = (1 - price) / price
    q = 1 - p_fair
    f = (p_fair * b - q) / b
    return max(0.0, f)


def size_position(
    side: str,
    p_fair_yes: float,
    market_price_yes: float,
    bankroll: float | None = None,
    kappa: float | None = None,
) -> SizingResult:
    """Calcula tamaño de la posición.

    side: "BUY_YES" → apuesta a que YES resuelve 1 a precio market_price_yes
          "BUY_NO"  → apuesta a que YES resuelve 0; precio efectivo es 1 - market_price_yes
                      y p_fair efectiva es 1 - p_fair_yes
    """
    bankroll = bankroll if bankroll is not None else settings.bankroll_usd
    kappa = kappa if kappa is not None else settings.kelly_kappa

    if side == "BUY_YES":
        p, price = p_fair_yes, market_price_yes
    elif side == "BUY_NO":
        p, price = 1 - p_fair_yes, 1 - market_price_yes
    else:
        raise ValueError(f"side desconocido: {side}")

    f_star = _kelly_fraction(p, price)
    notional = kappa * bankroll * f_star
    shares = notional / price if price > 0 else 0.0
    return SizingResult(f_star=f_star, shares=shares, notional_usd=notional)
