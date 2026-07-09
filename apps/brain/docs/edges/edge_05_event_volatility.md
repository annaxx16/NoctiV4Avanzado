# Edge 05 — Event Volatility (Volatilidad de Evento)

> **Estado**: NO IMPLEMENTADO. Requiere `time_to_resolution` + modelo de volatilidad pre-evento.

---

## Hipótesis

Antes de eventos conocidos y programados (debates electorales, decisiones de la Fed, veredictos judiciales, CPI, earnings políticos), el mercado sobre-hedgea: compra incertidumbre y empuja los precios hacia extremos. Esto genera una "prima de volatilidad" que se comprime rápidamente después del evento, cuando la incertidumbre se resuelve.

El edge: **vender la incertidumbre pre-evento** o **comprar la compresión post-evento**.

---

## Señal Matemática

```
σ_rolling_7d  = stdev(mid_price) calculado sobre los últimos 7 días
σ_rolling_1d  = stdev(mid_price) calculado sobre las últimas 24h
σ_base_30d    = stdev histórico de 30 días (nivel "normal" del mercado)

VolShock_ratio = σ_rolling_1d / σ_base_30d

time_to_event  = horas hasta end_date o evento detectado en calendario externo

Caso A — Pre-evento:
  Si VolShock_ratio > θ₅ (ej. 2.0) Y time_to_event < 24h:
    → Precio sobre-extendido por volatilidad; esperar evento y entrar post-resolución
    → No entrar hasta que el evento resuelva (demasiado riesgo binario)

Caso B — Post-evento (implementación recomendada):
  Si VolShock_ratio_pre > θ₅ Y evento_ocurrió_hace < 2h Y volatilidad_bajando:
    → BUY en dirección del resultado (convergencia post-evento)
    → O BUY_NO si la euforia post-evento es excesiva (combinar con Overreaction)
```

**Implementación simplificada para Mes 2** (sin calendario externo):
```
Si vol_z > 2.0 Y time_to_resolution < 12h Y |mid_velocity| disminuyendo:
  → señal de compresión post-evento
```

---

## Features Requeridos

| Feature | Fuente | Requiere |
|---|---|---|
| `time_to_resolution_h` | `markets.end_date - as_of` | Ya disponible en Gamma metadata |
| `vol_z` | `features/calculator.py` | Ya implementado |
| `sigma_rolling_1d` | Calculado sobre snapshots de 24h | DuckDB o ventana larga |
| `sigma_base_30d` | Calculado sobre snapshots de 30d | DuckDB histórico |
| `momentum_deceleration` | `velocity(t) - velocity(t-5m)` | Feature nuevo |
| `event_calendar_hit` | ETL: calendario de eventos programados | Opcional (Mes 3) |

---

## Parámetros Configurables

| Parámetro | Valor sugerido | Descripción |
|---|---|---|
| `ev_vol_shock_threshold` | 2.0 | VolShock_ratio mínimo para señal |
| `ev_max_time_to_resolution_h` | 12.0 | Solo operar cuando queda <12h para resolución |
| `ev_min_time_to_resolution_h` | 2.0 | No operar en las últimas 2h (ya configurado en Risk Engine) |
| `ev_deceleration_threshold` | -0.00001 | Deceleration debe ser negativa (tendencia perdiendo fuerza) |

---

## Pseudocódigo

```python
def detect_event_volatility(
    condition_id: str,
    as_of: datetime,
    snapshots: list[SnapshotInput],
    time_to_resolution_h: float,
    vol_shock_threshold: float = 2.0,
    max_hours_to_event: float = 12.0,
    min_hours_to_event: float = 2.0,
) -> EdgeOutput | None:
    
    # Filtro temporal: solo operar en ventana pre-resolución
    if not (min_hours_to_event <= time_to_resolution_h <= max_hours_to_event):
        return None
    
    history = [s for s in snapshots if s.ts <= as_of]
    mids = [mid(s) for s in history if mid(s) is not None]
    if len(mids) < 50:  # necesitamos historia larga para baseline
        return None
    
    # Baseline: std de todo el histórico disponible (proxy de 30d)
    baseline_std = stdev(mids[:-int(len(mids)*0.1)])  # excluir último 10%
    
    # Volatilidad reciente: std de últimas 24h de snapshots
    cutoff_1d = as_of - timedelta(hours=24)
    recent_mids = [m for s, m in zip(history, mids) if s.ts >= cutoff_1d]
    if len(recent_mids) < 10:
        return None
    recent_std = stdev(recent_mids)
    
    if baseline_std <= 0:
        return None
    
    vol_shock_ratio = recent_std / baseline_std
    if vol_shock_ratio < vol_shock_threshold:
        return None  # no hay volatilidad elevada
    
    # Dirección: apostar a compresión (precio revierte hacia media larga)
    market_price = mids[-1]
    fair_price = statistics.mean(mids[-20:])  # media reciente como fair
    side = "BUY_NO" if market_price > fair_price else "BUY_YES"
    
    return EdgeOutput(
        edge_name="event_volatility",
        side=side,
        market_price=market_price,
        fair_price=fair_price,
        edge_value=abs(market_price - fair_price),
        strength=vol_shock_ratio,
        reason=f"vol_shock={vol_shock_ratio:.2f} tte={time_to_resolution_h:.1f}h",
    )
```

---

## Criterios de Validación

| Métrica | Umbral |
|---|---|
| EV por señal | > 0.005 (debe ser > Overreaction dado el riesgo binario residual) |
| Brier score | < 0.22 (este edge debe ser más preciso que base) |
| Max posición simultánea en este edge | ≤ $30 (reducción por riesgo binario) |
| Combinación con Exit Engine TTL | TTL corto (≤ 4h) para posiciones de este edge |

**Riesgo principal**: si el evento resuelve de forma binaria antes de que el precio revierte, la pérdida es total. Por eso `max_risk_per_trade_usd` debe ser más bajo para este edge (override en orchestrator).

---

## Pre-requisitos de Implementación

1. Campo `time_to_resolution_h` calculado y cacheado en Redis con cada snapshot.
2. Ventana de snapshots de 24h accesible sin full scan (índice en `book_snapshots.ts`).
3. El orchestrator pasa `time_to_resolution_h` a cada edge que lo requiera.
