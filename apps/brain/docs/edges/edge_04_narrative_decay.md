# Edge 04 — Narrative Decay (Decaimiento de Narrativa)

> **Estado**: NO IMPLEMENTADO. Requiere ETL externo (mentions/hora).

---

## Hipótesis

Las narrativas emocionales tienen vida corta. El retail extrapola linealmente el impacto de un evento: cuando Trump publica un tweet o hay un escándalo, el precio salta y se mantiene inflado mientras la narrativa está "viva". Pero la atención del público decae rápidamente (ciclo de 24-48 horas). El mercado tarda en normalizar porque la posición ya está tomada y nadie quiere admitir el error.

El edge: apostar a la normalización **cuando la narrativa ya está decayendo pero el precio todavía no lo refleja**.

---

## Señal Matemática

```
mentions_now   = menciones/hora del keyword del mercado en T_actual
mentions_6h    = menciones/hora en T - 6h

ND = mentions_now / mentions_6h  (Narrative Decay Ratio)

Si ND < θ₄ (ej. 0.30, es decir la narrativa cayó al 30% de su pico de 6h):
  Y el precio sigue > EMA_larga (aún inflado):
    → BUY_NO (apostar que el precio baja hacia su media)

Si ND < θ₄ Y el precio sigue < EMA_larga (aún deflado):
    → BUY_YES (apostar que el precio sube hacia su media)
```

**Confirmación adicional**: que el precio se haya movido significativamente en las últimas 6h (delta_p_6h > threshold). Si el precio no se movió, no hay narrativa que decaer.

---

## Features Requeridos

| Feature | Fuente | Requiere |
|---|---|---|
| `mentions_per_hour` | ETL externo (Twitter API, RSS) | Pipeline ETL |
| `mentions_6h_ago` | ETL externo + histórico Redis | Pipeline ETL |
| `mentions_decay_ratio` | Calculado: mentions_now / mentions_6h | ETL |
| `delta_p_6h` | Calculado sobre snapshots de 6h atrás | DuckDB histórico o Redis |
| `ema_long` | EMA con alpha=0.03 (más suave que OverreactionV1) | Calculado sobre snapshots |
| `sentiment_score` | NLP sobre menciones recientes | Modelo NLP |

---

## Parámetros Configurables

| Parámetro | Valor sugerido | Descripción |
|---|---|---|
| `narrative_decay_ratio_threshold` | 0.30 | ND < 0.30 indica decaimiento severo |
| `narrative_min_initial_mentions` | 50 | Solo activar si hubo un pico real (≥50 menciones/hora) |
| `narrative_price_delta_min` | 0.05 | El precio debe haberse movido ≥5pp en las últimas 6h |
| `narrative_ema_alpha_long` | 0.03 | EMA suave de largo plazo |

---

## Pseudocódigo

```python
def detect_narrative_decay(
    condition_id: str,
    as_of: datetime,
    mentions_timeseries: list[tuple[datetime, float]],  # (ts, menciones/hora)
    snapshots: list[SnapshotInput],
    threshold: float = 0.30,
    min_initial_mentions: float = 50.0,
    min_price_delta: float = 0.05,
) -> EdgeOutput | None:
    
    now_mentions = get_mentions_at(mentions_timeseries, as_of)
    past_mentions = get_mentions_at(mentions_timeseries, as_of - timedelta(hours=6))
    
    # Sin datos de menciones: no operar
    if now_mentions is None or past_mentions is None:
        return None
    
    # Solo activar si hubo pico real
    if past_mentions < min_initial_mentions:
        return None
    
    # El ratio debe indicar decaimiento significativo
    if past_mentions == 0:
        return None
    nd_ratio = now_mentions / past_mentions
    if nd_ratio >= threshold:
        return None
    
    # El precio debe haber reaccionado a la narrativa original
    history = [s for s in snapshots if s.ts <= as_of]
    mids = [mid(s) for s in history if mid(s) is not None]
    if len(mids) < 20:
        return None
    
    price_now = mids[-1]
    past_snap = get_snapshot_at(history, as_of - timedelta(hours=6))
    if past_snap is None:
        return None
    price_6h = mid(past_snap)
    
    if abs(price_now - price_6h) < min_price_delta:
        return None  # no hubo movimiento relevante
    
    # EMA larga = nivel "justo" previo a la narrativa
    ema_long = ema(mids[:-1], alpha=0.03)
    
    # Precio aún desviado de EMA larga
    deviation = price_now - ema_long
    if abs(deviation) < 0.03:
        return None  # ya casi normalizó
    
    side = "BUY_NO" if deviation > 0 else "BUY_YES"
    strength = abs(deviation) * (1.0 - nd_ratio)  # más fuerte cuanto más decayó
    
    return EdgeOutput(
        edge_name="narrative_decay",
        side=side,
        market_price=price_now,
        fair_price=ema_long,
        edge_value=abs(deviation),
        strength=strength,
        reason=f"nd_ratio={nd_ratio:.2f} deviation={deviation:+.4f}",
    )
```

---

## Criterios de Validación

| Métrica | Umbral |
|---|---|
| EV por señal | > 0.003 |
| Tiempo promedio hasta normalización | < 48h (si > esto, el edge tarda demasiado) |
| Brier score | < 0.25 |
| Frecuencia de señales | > 5/semana |

**Riesgo principal**: el decaimiento de menciones puede no implicar decaimiento de precio si la narrativa tiene implicaciones estructurales (ej. un cambio regulatorio real, no solo un tweet). Filtrar por `sentiment_type`: solo aplicar a narrativas emocionales/virales, no a eventos fundamentales.

---

## Pre-requisitos de Implementación

1. Pipeline ETL externo con contador de menciones por keyword cada hora.
2. Tabla `mentions_timeseries` (condition_id, ts, mentions_per_hour, source).
3. Tests: mock de timeseries con pico a las T-6h y decaimiento a T → señal correcta.
