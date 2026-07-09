# Edge 08 — Spread Expansion (Expansión de Spread)

> **Estado**: NO IMPLEMENTADO como edge separado. La feature `spread_expansion` ya está calculada en `features/calculator.py`. Solo falta la lógica de señal.

---

## Hipótesis

Cuando el spread bid-ask se expande significativamente respecto a su nivel histórico, indica que los market makers están ampliando márgenes por incertidumbre temporal. Esta incertidumbre se resuelve rápidamente: el spread vuelve a su nivel normal una vez que el mercado absorbe la información o la orden grande se ejecuta.

El edge aprovecha dos situaciones:
1. **Timing de entrada**: esperar a que el spread se contraiga para entrar con menor costo de slippage.
2. **Señal direccional**: un spread expansión asimétrica (más en asks que en bids, o viceversa) indica presión compradora/vendedora.

---

## Señal Matemática

```
spread_expansion = (spread(t) - μ_spread_5m) / σ_spread_5m   (ya calculado)

Caso A — Timing de entrada (mejorar slippage):
  Cuando spread_expansion > θ₈ (ej. 2.0 sigmas):
    → No entrar ahora; marcar el mercado como "pendiente de contracción"
    → Cuando spread_expansion < 0.5 en el siguiente poll: entrar con la señal pendiente

Caso B — Señal direccional (spread asimétrico):
  ask_spread = ask - mid
  bid_spread = mid - bid
  asymmetry = (ask_spread - bid_spread) / spread
  
  Si asymmetry > 0.3 (asks más amplios → presión compradora):
    → BUY_YES (demanda está empujando asks al alza)
  Si asymmetry < -0.3 (bids más amplios → presión vendedora):
    → BUY_NO
```

---

## Features Requeridos

| Feature | Fuente | Requiere |
|---|---|---|
| `spread_expansion` | `features/calculator.py` | Ya implementado |
| `spread` | `features/calculator.py` | Ya implementado |
| `best_bid` | `SnapshotInput` | Ya disponible |
| `best_ask` | `SnapshotInput` | Ya disponible |

**Este edge puede implementarse sin dependencias nuevas.**

---

## Parámetros Configurables

| Parámetro | Valor sugerido | Descripción |
|---|---|---|
| `spread_expansion_threshold` | 2.0 | z-score del spread para señal |
| `spread_asymmetry_threshold` | 0.30 | Asimetría mínima para señal direccional |
| `spread_cooldown_polls` | 3 | Esperar 3 polls (90s) después de detectar expansión antes de entrar |

---

## Pseudocódigo

```python
@dataclass
class SpreadSignalState:
    """Estado por mercado: tracking de expansiones recientes."""
    pending_side: str | None = None
    expansion_detected_at: datetime | None = None
    expansion_magnitude: float = 0.0

# En el orchestrator: mantener SpreadSignalState por condition_id en Redis

def detect_spread_expansion(
    features: FeatureSet,
    state: SpreadSignalState,
    as_of: datetime,
    expansion_threshold: float = 2.0,
    asymmetry_threshold: float = 0.30,
    cooldown_polls: int = 3,
) -> EdgeOutput | None:
    
    if features.spread_expansion is None or features.spread is None:
        return None
    
    mid = features.mid_price
    if mid is None:
        return None
    
    # Caso B: señal direccional por asimetría
    # Para esto necesitamos best_bid y best_ask del snapshot actual
    # (pasados como parámetro extra desde el orchestrator)
    # ...
    
    # Caso A: timing de entrada
    if features.spread_expansion > expansion_threshold:
        # Expansión detectada; NO entrar ahora, registrar estado
        return None  # diferir entrada
    
    # ¿Había una expansión pendiente y ahora el spread se contrajo?
    if (
        state.pending_side is not None
        and features.spread_expansion < 0.5
        and state.expansion_detected_at is not None
    ):
        delay_min = (as_of - state.expansion_detected_at).total_seconds() / 60
        if delay_min <= 10:  # la contracción ocurrió dentro de 10 minutos
            # Entrar con la señal que teníamos pendiente
            return EdgeOutput(
                edge_name="spread_expansion",
                side=state.pending_side,
                market_price=features.mid_price,
                fair_price=features.mid_price,
                edge_value=state.expansion_magnitude / 10.0,
                strength=state.expansion_magnitude,
                reason=f"spread_contracted after expansion={state.expansion_magnitude:.2f}σ",
            )
    
    return None
```

---

## Implementación Simplificada (Mes 2)

Para empezar sin gestión de estado compleja:

```python
def detect_spread_expansion_simple(
    features: FeatureSet,
    threshold: float = 2.0,
) -> EdgeOutput | None:
    """
    Versión sin estado: señal cuando el spread se normaliza después de expansión.
    Combinar con Overreaction para confirmar dirección.
    """
    if features.spread_expansion is None:
        return None
    
    # Spread en normalización (negativo o cerca de cero después de haber sido alto)
    # Esto requiere comparar con el poll anterior; usar mid_velocity como proxy
    if -0.5 < features.spread_expansion < 0.5 and abs(features.mid_velocity or 0) < 0.0002:
        # Spread en reposo; mercado estabilizado
        # Este edge en modo simple solo actúa como FILTRO de calidad para otros edges
        return None  # No genera señal sola; confirma otras señales
    
    return None
```

**Decisión de diseño**: en la implementación inicial, `spread_expansion` actúa principalmente como **filtro confirmador** del Risk Engine (ya implementado en `risk/engine.py` via `max_spread_for_entry`), no como edge independiente. Se puede promover a edge autónomo cuando tengamos datos históricos suficientes para calibrar el threshold direccional.

---

## Criterios de Validación

| Métrica | Umbral |
|---|---|
| Reducción de slippage vs. entrar sin filtro | > 15 bps promedio |
| EV incremental vs. Overreaction sin filtro | > 0 (debe mejorar, no empeorar) |
| % de expansiones que se contraen en < 10 min | > 70% |

---

## Estado de Implementación

El Risk Engine actual ya filtra spreads > 4% (`max_spread_for_entry`). Este edge añade la dimensión **timing**: esperar la contracción activamente en vez de rechazar la señal.

Prioritario solo después de validar Overreaction. La feature ya existe.
