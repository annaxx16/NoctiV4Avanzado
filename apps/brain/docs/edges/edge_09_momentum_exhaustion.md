# Edge 09 — Momentum Exhaustion (Agotamiento de Momentum)

> **Estado**: NO IMPLEMENTADO. La feature `mid_velocity` ya existe. Solo falta calcular aceleración y detectar el patrón.

---

## Hipótesis

Las tendencias de precio pierden aceleración antes de revertir. Cuando un movimiento fuerte empieza a desacelerar (la velocidad sigue siendo positiva pero la aceleración se vuelve negativa), es señal de que el momentum se está agotando. Los operadores que entraron en el movimiento empiezan a tomar ganancias, reduciendo la presión.

Este edge complementa a OverreactionV1: Overreaction detecta el spike después de que ocurrió; Momentum Exhaustion detecta **el final del spike en tiempo real**, potencialmente entrando un poco antes.

---

## Señal Matemática

```
velocity(t)      = mid_velocity en t  (ya calculado)
velocity(t-5m)   = mid_velocity en t - 5 minutos

acceleration = velocity(t) - velocity(t-5m)

Exhaustion score:
  Si velocity(t) tiene signo definido (tendencia clara):
    Y acceleration < θ₉ (negativo: desacelerando)
    Y |velocity(t)| > velocity_min (hubo movimiento real):
      → señal contra-tendencia (inversa al signo de velocity)
```

---

## Features Requeridos

| Feature | Fuente | Requiere |
|---|---|---|
| `mid_velocity` | `features/calculator.py` | Ya implementado |
| `velocity_5m_ago` | Calculado sobre snapshots históricos | Requiere ventana de snapshots |
| `momentum_deceleration` | `mid_velocity(t) - mid_velocity(t-5m)` | Feature nuevo |
| `delta_p_5m` | `features/calculator.py` | Ya implementado (confirmación) |

---

## Parámetros Configurables

| Parámetro | Valor sugerido | Descripción |
|---|---|---|
| `exhaustion_min_velocity` | 0.0001 | Velocidad mínima para que haya momentum real |
| `exhaustion_deceleration_threshold` | -0.00005 | Aceleración debe ser < este valor (negativa) |
| `exhaustion_min_price_move` | 0.02 | El precio debe haberse movido ≥2pp en 5m |

---

## Pseudocódigo

```python
def detect_momentum_exhaustion(
    snapshots: list[SnapshotInput],
    features: FeatureSet,
    as_of: datetime,
    min_velocity: float = 0.0001,
    decel_threshold: float = -0.00005,
    min_price_move: float = 0.02,
) -> EdgeOutput | None:
    
    velocity_now = features.mid_velocity
    if velocity_now is None or abs(velocity_now) < min_velocity:
        return None  # no hay tendencia real
    
    # Calcular velocidad hace 5 minutos
    past_5m_snaps = [s for s in snapshots if s.ts <= as_of - timedelta(minutes=5)]
    if len(past_5m_snaps) < 2:
        return None
    
    # Velocidad en t-5m: usar últimos 2 snapshots de esa ventana
    last_prev = past_5m_snaps[-1]
    prev_prev = past_5m_snaps[-2]
    mid_last_prev = mid(last_prev)
    mid_prev_prev = mid(prev_prev)
    
    if mid_last_prev is None or mid_prev_prev is None:
        return None
    
    dt_prev = (last_prev.ts - prev_prev.ts).total_seconds()
    if dt_prev <= 0:
        return None
    
    velocity_5m_ago = (mid_last_prev - mid_prev_prev) / dt_prev
    acceleration = velocity_now - velocity_5m_ago
    
    # Aceleración debe ser negativa (desacelerando)
    if acceleration >= decel_threshold:
        return None
    
    # El movimiento debe haber sido real
    if features.delta_p_5m is None or abs(features.delta_p_5m) < min_price_move:
        return None
    
    # Señal: contra-tendencia (el momentum se está agotando)
    side = "BUY_NO" if velocity_now > 0 else "BUY_YES"
    
    # Fair price: mid en t-5m como referencia de reversión
    fair_price = mid(past_5m_snaps[-1]) or features.mid_price
    
    strength = abs(acceleration) / abs(velocity_5m_ago) if velocity_5m_ago != 0 else 0
    
    return EdgeOutput(
        edge_name="momentum_exhaustion",
        side=side,
        market_price=features.mid_price,
        fair_price=fair_price,
        edge_value=abs(features.mid_price - fair_price),
        strength=strength,
        reason=f"velocity={velocity_now:.6f} accel={acceleration:.6f}",
    )
```

---

## Relación con OverreactionV1

| Aspecto | OverreactionV1 | Momentum Exhaustion |
|---|---|---|
| Cuándo activa | Después de que el spike ocurrió completamente | Durante la desaceleración del spike |
| Requiere | ≥10 snapshots, σ > 3 | ≥5 snapshots, aceleración negativa |
| Sensibilidad | Menor (umbral σ alto) | Mayor (puede activar en spikes más pequeños) |
| Falsos positivos | Menos | Más (tendencias pausas ≠ reversal) |
| Combinación óptima | Ambos dicen lo mismo → mayor convicción | Ambos dicen lo mismo → mayor convicción |

---

## Criterios de Validación

| Métrica | Umbral |
|---|---|
| EV por señal | > 0.002 (menor que Overreaction; mayor frecuencia) |
| Falsos positivos | < 40% (aceleración negativa ≠ siempre reversión) |
| Complementariedad con Edge 01 | Correlación < 0.7 con señales de Overreaction |

**Riesgo principal**: una desaceleración temporal dentro de una tendencia fuerte se interpreta como exhaustion cuando en realidad es una pausa. El filtro `min_price_move` y la confirmación de `spread_expansion` mitigan esto.

---

## Implementación Inmediata

Este edge puede implementarse **ahora** sin dependencias externas. La única adición al `features/calculator.py` es calcular `velocity_5m_ago` y `momentum_deceleration`, que solo requieren la ventana de snapshots ya disponible.

```python
# Añadir a FeatureSet:
momentum_deceleration: float | None  # velocity(t) - velocity(t-5m)

# Añadir a calculate_features():
def _momentum_deceleration(snapshots, as_of):
    velocity_now = _mid_velocity(snapshots)
    past_snaps = [s for s in snapshots if s.ts <= as_of - timedelta(minutes=5)]
    velocity_past = _mid_velocity(past_snaps) if len(past_snaps) >= 2 else None
    if velocity_now is None or velocity_past is None:
        return None
    return velocity_now - velocity_past
```
