# Edge 10 — Probability Drift (Deriva de Probabilidad)

> **Estado**: NO IMPLEMENTADO. Requiere histórico largo en DuckDB (varios meses de snapshots).

---

## Hipótesis

Los precios de contratos de predicción exhiben mean reversion respecto a su media histórica de largo plazo. Eventos que empujan temporalmente el precio a extremos (0.85, 0.90+) tienden a revertir, especialmente cuando no hay nueva información estructural. El mercado sobre-estima el impacto de eventos recientes y sub-estima la incertidumbre residual.

Este edge es el "primo cuantitativo" de Overreaction: mientras Overreaction usa la EMA de corto plazo (minutos), Probability Drift usa la distribución histórica de largo plazo (días/semanas).

---

## Señal Matemática

```
μ_hist    = media histórica del mid_price de este mercado (últimos 30d)
σ_hist    = desviación estándar histórica (últimos 30d)

Drift_z  = (market_price - μ_hist) / σ_hist

Si Drift_z > +2.0:
  → precio en extremo superior histórico → BUY_NO (apostar que baja hacia media)
Si Drift_z < -2.0:
  → precio en extremo inferior histórico → BUY_YES (apostar que sube hacia media)

Confirmación adicional:
  Y el precio lleva > 24h alejado del rango [-1σ, +1σ] (no es un spike nuevo)
  Y el tiempo hasta resolución > 48h (hay suficiente tiempo para mean reversion)
```

---

## Features Requeridos

| Feature | Fuente | Requiere |
|---|---|---|
| `prob_z_historical` | DuckDB: media/std de últimos 30d | DuckDB histórico (GAP histórico) |
| `days_in_extreme` | Cuántos días consecutivos fuera de ±1σ | Cálculo sobre DuckDB |
| `time_to_resolution_h` | `markets.end_date - as_of` | Ya disponible |
| `mid_price` | `features/calculator.py` | Ya implementado |

---

## Parámetros Configurables

| Parámetro | Valor sugerido | Descripción |
|---|---|---|
| `drift_z_threshold` | 2.0 | Z-score histórico para señal |
| `drift_min_days_extreme` | 1 | Mínimo 1 día en el extremo (no spike nuevo) |
| `drift_min_tte_hours` | 48 | Tiempo mínimo hasta resolución |
| `drift_lookback_days` | 30 | Ventana histórica para μ y σ |

---

## Pseudocódigo

```python
@dataclass
class ProbabilityDriftContext:
    historical_mean: float
    historical_std: float
    days_in_extreme: int

async def load_drift_context(
    duckdb_conn,
    condition_id: str,
    as_of: datetime,
    lookback_days: int = 30,
) -> ProbabilityDriftContext | None:
    """Consultar DuckDB para estadísticas históricas."""
    cutoff = as_of - timedelta(days=lookback_days)
    
    result = duckdb_conn.execute("""
        SELECT
          AVG((best_bid + best_ask) / 2.0) AS mean_mid,
          STDDEV((best_bid + best_ask) / 2.0) AS std_mid
        FROM book_snapshots
        WHERE market_id = ?
          AND ts BETWEEN ? AND ?
          AND best_bid IS NOT NULL
          AND best_ask IS NOT NULL
    """, [condition_id, cutoff, as_of]).fetchone()
    
    if result is None or result[1] is None or result[1] == 0:
        return None
    
    # Contar días consecutivos fuera de ±1σ
    mean, std = result[0], result[1]
    extreme_query = duckdb_conn.execute("""
        SELECT COUNT(DISTINCT DATE(ts)) AS extreme_days
        FROM book_snapshots
        WHERE market_id = ?
          AND ts BETWEEN ? AND ?
          AND ABS(((best_bid + best_ask) / 2.0) - ?) > ?
    """, [condition_id, as_of - timedelta(days=7), as_of, mean, std]).fetchone()
    
    return ProbabilityDriftContext(
        historical_mean=mean,
        historical_std=std,
        days_in_extreme=extreme_query[0] if extreme_query else 0,
    )


def detect_probability_drift(
    features: FeatureSet,
    context: ProbabilityDriftContext,
    time_to_resolution_h: float,
    z_threshold: float = 2.0,
    min_days_extreme: int = 1,
    min_tte_hours: float = 48.0,
) -> EdgeOutput | None:
    
    if time_to_resolution_h < min_tte_hours:
        return None  # muy cercano a resolución; mean reversion puede no ocurrir
    
    if context.days_in_extreme < min_days_extreme:
        return None  # puede ser un spike reciente, no drift sostenido
    
    if context.historical_std <= 0 or features.mid_price is None:
        return None
    
    drift_z = (features.mid_price - context.historical_mean) / context.historical_std
    
    if abs(drift_z) < z_threshold:
        return None
    
    side = "BUY_NO" if drift_z > 0 else "BUY_YES"
    
    return EdgeOutput(
        edge_name="probability_drift",
        side=side,
        market_price=features.mid_price,
        fair_price=context.historical_mean,
        edge_value=abs(features.mid_price - context.historical_mean),
        strength=abs(drift_z),
        reason=f"drift_z={drift_z:.2f} hist_mean={context.historical_mean:.4f} days_extreme={context.days_in_extreme}",
    )
```

---

## Consideraciones Importantes

### Mercados con precio naturalmente extremo

Algunos mercados están en 0.85+ porque genuinamente es casi seguro que YES resuelva. Este edge no debe aplicarse en esos casos. **Filtro adicional sugerido**: solo aplicar si el mercado ha tenido precio dentro de [0.30, 0.70] en algún momento de los últimos 14 días.

```python
if context.historical_mean < 0.20 or context.historical_mean > 0.80:
    return None  # mercado estructuralmente extremo; no hay reversion
```

### Time decay de Polymarket

Cerca de la resolución, el precio converge al resultado real (no a la media histórica). Por eso el filtro `min_tte_hours = 48` es crítico. Ignorarlo destruiría el EV.

---

## Criterios de Validación

| Métrica | Umbral |
|---|---|
| EV por señal | > 0.005 (debe compensar el tiempo de holding) |
| Brier score | < 0.22 |
| Tiempo promedio hasta reversión | < 7 días |
| % de señales donde precio vuelve a ±1σ en 7d | > 55% |

---

## Pre-requisitos de Implementación

1. Mínimo 30 días de snapshots acumulados antes de activar este edge.
2. DuckDB configurado con los datos exportados.
3. Función `load_drift_context()` cacheada en Redis (TTL 4h) para no recalcular en cada poll.
