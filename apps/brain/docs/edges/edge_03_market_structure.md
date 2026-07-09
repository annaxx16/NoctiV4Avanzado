# Edge 03 — Market Structure (Inconsistencia Probabilística)

> **Estado**: NO IMPLEMENTADO. Solo requiere datos internos de Polymarket. **Primer edge nuevo a implementar** tras validar Edge 01.

---

## Hipótesis

En mercados de predicción con múltiples outcomes relacionados, la suma de probabilidades puede diferir significativamente de 1.0. Esta inconsistencia crea oportunidades de arbitraje: el contrato sub-valuado (o sobre-valuado) eventualmente convergerá hacia la coherencia matemática.

**Ejemplos**:
- "¿Partido A gana las elecciones?" = 70%, "¿Partido B gana?" = 40%. Son mutuamente excluyentes → suma = 110%. El spread implícito es 10%.
- "¿El precio de BTC supera $100k en 2025?" YES = 65%, NO = 60% → suma = 125%. El NO está sobre-valuado.

---

## Señal Matemática

Para un conjunto de mercados mutuamente excluyentes y exhaustivos:

```
Inconsistency = |ΣP_i - 1.0|

Si Inconsistency > θ₃ (ej. 0.03, es decir 3pp):
  → identificar el contrato más desviado de su valor justo
  → fair_i = P_i / ΣP_j  (normalización proporcional)
  → edge_i = |P_i - fair_i|
  → señal BUY si P_i < fair_i (infravalorado) o SELL si P_i > fair_i

Para mercados binarios (YES/NO del mismo contrato):
  Inconsistency = |(P_yes + P_no) - 1.0|
  Esto es simplemente el spread mid: (ask_yes - bid_yes) + (ask_no - bid_no) - algo extra
  Indica ineficiencia si es > θ₃
```

---

## Features Requeridos

| Feature | Fuente | Requiere |
|---|---|---|
| `mid_price` de múltiples outcomes | `features/calculator.py` (por condition_id) | Ya disponible |
| `market_group_id` | `markets` tabla (campo a añadir) | Clasificación de grupos |
| `outcomes[]` | `markets.outcomes` (ya en Pydantic schema) | Ya disponible |
| `outcomePrices[]` | `polymarket/schemas.py` (ya parseado) | Ya disponible |

---

## Parámetros Configurables

| Parámetro | Valor sugerido | Descripción |
|---|---|---|
| `structure_inconsistency_threshold` | 0.03 | 3% de inconsistencia como mínimo |
| `structure_min_group_size` | 2 | Mínimo 2 outcomes en el grupo |
| `structure_max_spread_per_leg` | 0.04 | No entrar si el spread de cualquier leg es > 4% |

---

## Pseudocódigo

```python
@dataclass
class MarketGroup:
    group_id: str
    markets: list[tuple[str, float]]  # (condition_id, mid_price)
    exclusive: bool  # ¿mutuamente excluyentes?

def detect_market_structure(
    groups: list[MarketGroup],
    as_of: datetime,
    threshold: float = 0.03,
) -> list[EdgeOutput]:
    signals = []
    
    for group in groups:
        if len(group.markets) < 2:
            continue
        
        prices = [price for _, price in group.markets]
        total = sum(prices)
        inconsistency = abs(total - 1.0)
        
        if inconsistency < threshold:
            continue
        
        # Normalizar: fair_i = P_i / total
        fair_prices = [p / total for p in prices]
        
        for (condition_id, market_price), fair_price in zip(group.markets, fair_prices):
            edge_value = market_price - fair_price
            if abs(edge_value) < threshold / len(group.markets):
                continue  # contribución menor al umbral
            
            side = "BUY_YES" if market_price < fair_price else "BUY_NO"
            signals.append(EdgeOutput(
                edge_name="market_structure",
                side=side,
                market_price=market_price,
                fair_price=fair_price,
                edge_value=abs(edge_value),
                strength=inconsistency,
                reason=f"inconsistency={inconsistency:.4f} sum={total:.4f}",
            ))
    
    return signals
```

---

## Construcción de Grupos

El mayor desafío es identificar qué mercados pertenecen al mismo "grupo":

```python
def build_market_groups(markets: list[GammaMarket]) -> list[MarketGroup]:
    """
    Estrategia 1 (rápida): agrupar por event_id de Gamma
    Estrategia 2 (semántica): clustering de embeddings de market.question
    """
    groups: dict[str, list] = {}
    for m in markets:
        key = m.event_id or m.slug.rsplit("-", 1)[0]  # heurística por slug
        groups.setdefault(key, []).append((m.condition_id, m.mid_price))
    
    return [
        MarketGroup(group_id=k, markets=v, exclusive=len(v) > 1)
        for k, v in groups.items()
        if len(v) >= 2
    ]
```

---

## Criterios de Validación

| Métrica | Umbral |
|---|---|
| EV por señal | > 0.004 |
| Frecuencia de señales | > 10/semana (si < esto, el universo no tiene suficientes grupos) |
| Convergencia observada | La inconsistencia se reduce en t+30m en >60% de los casos |
| Profit Factor | > 1.3 |

**Riesgo principal**: la inconsistencia puede deberse a spreads naturales, no a ineficiencia. El filtro `max_spread_per_leg` es crítico para no operar cuando el spread ya explica la inconsistencia.

---

## Pre-requisitos de Implementación

1. Campo `event_id` en tabla `markets` (o agrupación por slug).
2. Script periódico que identifica grupos y llena `market_groups` table.
3. El orchestrator evalúa grupos en vez de mercados individuales para este edge.
4. Tests: mock de 3 mercados con inconsistencia de 5% → señal correcta detectada.
