# Edge 01 — Overreaction (OverreactionV1)

> **Estado**: IMPLEMENTADO y activo en `src/umbra/edges/overreaction.py`

---

## Hipótesis

El retail sobre-reacciona a noticias, anuncios y eventos emocionales. Compra pánico, vende euforia. Esto genera **spikes de precio irracionales** que se desvían temporalmente de la tendencia subyacente y luego revierten parcialmente hacia la EMA previa. El edge captura esa reversión.

La EMA de los snapshots anteriores al spike sirve como proxy del "fair price" pre-reacción. Si el precio actual se aleja más de N desviaciones estándar de esa EMA, la hipótesis es que hay sobre-extensión y el mercado revertirá.

---

## Señal Matemática

```
fair_price = EMA(mid_history[:-1], alpha=0.1)
recent_std = stdev(últimos 10 mids anteriores al actual)

sigma = (market_price - fair_price) / recent_std

Si sigma > +3  → BUY_NO  (precio inflado; apostar que baja)
Si sigma < -3  → BUY_YES (precio deflado; apostar que sube)
Si |sigma| < 3 → sin señal
```

**Por qué excluir el punto actual del cálculo de std**: incluirlo inflaría el std con la propia magnitud del spike, enmascarando la sobre-reacción y reduciendo la sensibilidad del detector.

---

## Fórmula ORS Extendida (objetivo Mes 2)

La versión actual usa solo `sigma`. La versión extendida añade confirmación de volumen y sentimiento:

```
ORS = 0.50 · sigma_normalizado
    + 0.25 · vol_spike_indicator   (vol_z > 2 → 1.0; < 1 → 0.0; lineal entre)
    + 0.15 · velocity_indicator    (|mid_velocity| normalizado)
    + 0.10 · spread_expansion_ok   (spread no blow-out; 1 si spread_expansion < 2)

Señal si ORS > 0.6  (calibrar en backtest)
```

---

## Features Requeridos

| Feature | Fuente | Nota |
|---|---|---|
| `mid_price` | `features/calculator.py` | Precio actual |
| EMA sobre historia | Calculado en `edges/overreaction.py` | Sobre `mids[:-1]` |
| `recent_std` | Calculado en `edges/overreaction.py` | `stdev(last 10 mids[:-1])` |
| `vol_z` (ORS extendido) | `features/calculator.py` | z-score volumen 30m |
| `mid_velocity` (ORS extendido) | `features/calculator.py` | Derivada instantánea |
| `spread_expansion` (confirmación) | `features/calculator.py` | z-score spread 5m |

---

## Parámetros Configurables

| Parámetro | Valor actual | Rango de calibración |
|---|---|---|
| `overreaction_sigma_threshold` | 3.0 | 2.5 – 4.5 |
| `ema_alpha` | 0.1 | 0.05 – 0.20 |
| `overreaction_min_snapshots` | 10 | 8 – 20 |

**Regla de calibración**: solo ajustar `sigma_threshold` y `ema_alpha` en el análisis de sensibilidad. Máximo 2 parámetros simultáneos (STRATEGY.md).

---

## Pseudocódigo

```python
def detect(snapshots: list[SnapshotInput], as_of: datetime) -> EdgeOutput | None:
    history = [s for s in snapshots if s.ts <= as_of]
    mids = [mid(s) for s in history if mid(s) is not None]
    
    if len(mids) < MIN_SNAPSHOTS:
        return None
    
    market_price = mids[-1]
    hist = mids[:-1]  # excluir punto actual
    
    fair_price = ema(hist, alpha=EMA_ALPHA)
    recent_std = stdev(hist[-MIN_SNAPSHOTS:])
    
    if recent_std <= 0:
        return None
    
    sigma = (market_price - fair_price) / recent_std
    
    if abs(sigma) < SIGMA_THRESHOLD:
        return None
    
    # Sanity check: precio en rango válido de Polymarket
    if not (0.01 <= market_price <= 0.99):
        return None
    
    return EdgeOutput(
        side="BUY_NO" if sigma > 0 else "BUY_YES",
        market_price=market_price,
        fair_price=fair_price,
        edge_value=abs(fair_price - market_price),
        strength=sigma,
    )
```

---

## Criterios de Validación (Semana 1)

| Métrica | Umbral mínimo |
|---|---|
| Brier score | < 0.25 |
| EV por señal (después de slippage) | > 0 |
| Profit Factor | > 1.2 |
| Sharpe (paper 30d) | > 0.5 |
| Max Drawdown | < 15% |
| Degradación walk-forward | < 50% |

---

## Backtesting

**Datos requeridos**: `book_snapshots` de los últimos 90 días + tabla `outcomes` con resultados resueltos.

**Ventana de evaluación**: step de 5 minutos sobre histórico. Mínimo 500 señales evaluadas para significancia estadística.

**Grid de calibración** (máximo 2 parámetros):

```python
for sigma in [2.5, 3.0, 3.5, 4.0]:
    for alpha in [0.05, 0.10, 0.15, 0.20]:
        result = backtest_edge(
            sigma_threshold=sigma,
            ema_alpha=alpha,
            data=snapshots_train,
            outcomes=outcomes,
        )
        if result.ev > 0 and result.brier < 0.25:
            candidates.append((sigma, alpha, result))
# Elegir el más robusto (no el mejor); aplicar a test set sin reoptimizar.
```

---

## Archivos Relacionados

- Implementación: `src/umbra/edges/overreaction.py`
- Orchestrator: `src/umbra/engine/orchestrator.py`
- Tests: `tests/test_overreaction.py`, `tests/leakage/test_no_lookahead.py`
- Parámetros: `src/umbra/config.py` → `overreaction_sigma_threshold`, `ema_alpha`
