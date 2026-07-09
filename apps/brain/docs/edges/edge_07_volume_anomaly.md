# Edge 07 — Volume Anomaly (Anomalía de Volumen)

> **Estado**: NO IMPLEMENTADO. La feature `vol_z` ya está disponible en `features/calculator.py`. Solo falta la lógica del edge y la confirmación direccional.

---

## Hipótesis

Picos anormales de volumen (mucho más alto que la media histórica) indican actividad inusual: posiblemente insiders, rebalanceo de portfolios grandes, o información no pública. En mercados de predicción este volumen suele ser un indicador leading del movimiento de precio que vendrá.

A diferencia de Overreaction (que va contra el movimiento), este edge **sigue la dirección del volumen** cuando hay confirmación técnica (imbalance del book en la misma dirección).

---

## Señal Matemática

```
VolumeZ  = (vol_24h - μ_vol_30min) / σ_vol_30min  (ya calculado en features/calculator.py)
AccelVol = VolumeZ(t) - VolumeZ(t-5m)             (¿el volumen está acelerando?)

Condición de señal:
  VolumeZ > θ₇ (ej. 2.5)
  Y AccelVol > 0  (volumen en aumento, no disminuyendo)
  Y bid_ask_imbalance confirma la dirección:
    Si imbalance > 0 (más bids que asks) → BUY_YES
    Si imbalance < 0 (más asks que bids) → BUY_NO
  Y |delta_p_5m| < 0.03  (precio aún no reaccionó; anticipamos el movimiento)
```

---

## Features Requeridos

| Feature | Fuente | Requiere |
|---|---|---|
| `vol_z` | `features/calculator.py` | Ya implementado |
| `vol_acceleration` | `vol_z(t) - vol_z(t-5m)` | Feature nuevo (simple) |
| `bid_ask_imbalance` | CLOB API | CLOB API (opcional; sin esto, señal es más débil) |
| `delta_p_5m` | `features/calculator.py` | Ya implementado |

---

## Parámetros Configurables

| Parámetro | Valor sugerido | Descripción |
|---|---|---|
| `vol_anomaly_z_threshold` | 2.5 | VolumeZ mínimo para señal |
| `vol_anomaly_max_delta_5m` | 0.03 | Precio no debe haberse movido ya demasiado |
| `vol_anomaly_min_imbalance` | 0.15 | Si no hay CLOB, se omite este filtro |

---

## Pseudocódigo

```python
def detect_volume_anomaly(
    snapshots: list[SnapshotInput],
    features: FeatureSet,
    clob_snapshot: CLOBSnapshot | None,
    as_of: datetime,
    z_threshold: float = 2.5,
    max_delta_5m: float = 0.03,
) -> EdgeOutput | None:
    
    if features.vol_z is None or features.vol_z < z_threshold:
        return None
    
    # El precio no debe haber reaccionado ya
    if features.delta_p_5m is not None and abs(features.delta_p_5m) > max_delta_5m:
        return None
    
    # Calcular aceleración de volumen comparando con snapshot de 5m atrás
    history = [s for s in snapshots if s.ts <= as_of]
    past_5m = as_of - timedelta(minutes=5)
    past_snaps = [s for s in history if s.ts <= past_5m]
    if len(past_snaps) < 5:
        return None
    
    past_vols = [s.volume_24hr for s in past_snaps[-5:] if s.volume_24hr is not None]
    if len(past_vols) < 3:
        return None
    past_vol_mean = statistics.mean(past_vols)
    past_vol_std = statistics.stdev(past_vols) if len(past_vols) > 1 else 1.0
    
    current_vol = snapshots[-1].volume_24hr if snapshots else None
    if current_vol is None or past_vol_std == 0:
        return None
    
    vol_z_past = (past_vol_mean - past_vol_mean) / past_vol_std  # referencia relativa
    vol_acceleration = features.vol_z - (current_vol - past_vol_mean) / past_vol_std
    
    # Dirección: por book imbalance si disponible, si no por price momentum
    if clob_snapshot is not None:
        depth_bids = sum(s for _, s in clob_snapshot.bids[:5])
        depth_asks = sum(s for _, s in clob_snapshot.asks[:5])
        total = depth_bids + depth_asks
        if total > 0:
            imbalance = (depth_bids - depth_asks) / total
            side = "BUY_YES" if imbalance > 0.15 else ("BUY_NO" if imbalance < -0.15 else None)
        else:
            side = None
    else:
        # Sin CLOB: usar dirección del momentum reciente
        side = "BUY_YES" if (features.delta_p_5m or 0) > 0 else "BUY_NO"
    
    if side is None:
        return None  # sin dirección clara
    
    market_price = features.mid_price
    if market_price is None:
        return None
    
    return EdgeOutput(
        edge_name="volume_anomaly",
        side=side,
        market_price=market_price,
        fair_price=market_price,  # no hay fair; seguimos la dirección
        edge_value=features.vol_z / 10.0,  # normalizar a 0..1
        strength=features.vol_z,
        reason=f"vol_z={features.vol_z:.2f} accel={vol_acceleration:.2f}",
    )
```

---

## Criterios de Validación

| Métrica | Umbral |
|---|---|
| EV por señal | > 0.003 |
| Brier score | < 0.26 (este edge tiene más incertidumbre) |
| Hit rate | > 50% |
| % señales con movimiento de precio posterior | > 60% en <30min |

**Riesgo principal**: el volumen puede ser por rebalanceo de portfolios (no informado) o por ejecuciones programadas. El filtro de price_delta reduce falsas alarmas pero no las elimina.

**Nota importante**: este edge es complementario a Overreaction, no competitivo. Overreaction va contra el movimiento emocional; Volume Anomaly va con el movimiento informado. El Composite Edge (12) los distingue por contexto.

---

## Implementación Inmediata

Este edge puede implementarse ahora con solo `vol_z` y `delta_p_5m` (ya disponibles), sin CLOB API. La dirección se determina por momentum. Cuando se integre CLOB, se añade `bid_ask_imbalance` como confirmación.

```python
# Versión MVP sin CLOB (implementable hoy)
def detect_volume_anomaly_v1(
    features: FeatureSet,
    z_threshold: float = 2.5,
) -> EdgeOutput | None:
    if features.vol_z is None or features.vol_z < z_threshold:
        return None
    if features.delta_p_5m is None or abs(features.delta_p_5m) > 0.03:
        return None
    side = "BUY_YES" if features.delta_p_5m > 0 else "BUY_NO"
    return EdgeOutput(edge_name="volume_anomaly_v1", side=side, ...)
```
