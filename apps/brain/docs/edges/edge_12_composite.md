# Edge 12 — Composite Engine (Motor de Señales Compuesto)

> **Estado**: NO IMPLEMENTADO. Activar solo cuando ≥ 2 edges individuales estén validados.

---

## Hipótesis

Ningún edge individual es robusto en todos los regímenes de mercado. Un mercado político en campaña activa tiene overreaction alta, pero poca divergencia de consenso. Un mercado técnico tiene spread expansión alta pero poca narrativa. Combinar edges ortogonales mejora la robustez y reduce la dependencia en un patrón único.

El composite no es una suma ciega: **pondera por EV histórico**, penaliza el conflicto de dirección, y amplifica cuando múltiples edges coinciden.

---

## Señal Matemática

```
Para cada edge i activo y validado:
  score_i  ∈ [0, 1]  (normalización de strength del edge)
  side_i   ∈ {BUY_YES, BUY_NO}
  weight_i = EV_histórico_i / Σ(EV_histórico_j)  (pesos calibrados)

Composite_YES = Σ score_i * weight_i  para todos i donde side_i = BUY_YES
Composite_NO  = Σ score_i * weight_i  para todos i donde side_i = BUY_NO

Composite_score = max(Composite_YES, Composite_NO)
Composite_side  = BUY_YES si Composite_YES > Composite_NO, else BUY_NO

Reglas de conflicto:
  Si |Composite_YES - Composite_NO| < conflict_threshold:
    → Sin señal (demasiado ambiguo)
  Si solo 1 edge activo:
    → Usar ese edge directamente (composite con N=1 no aporta)
```

---

## Implementación

```python
@dataclass
class CompositeEdgeResult:
    side: str
    composite_score: float
    edges_contributing: list[str]
    edges_conflicting: list[str]
    confidence: float

class CompositeEngine:
    def __init__(self, weights: dict[str, float]):
        self.weights = weights  # calibrados en backtest, actualizados semanalmente

    def evaluate(
        self,
        edge_outputs: dict[str, EdgeOutput | None],
        conflict_threshold: float = 0.15,
        min_edges_active: int = 2,
    ) -> CompositeEdgeResult | None:
        
        active = {
            name: out
            for name, out in edge_outputs.items()
            if out is not None
        }
        
        if len(active) < min_edges_active:
            return None  # composite con pocos edges no aporta
        
        score_yes = 0.0
        score_no = 0.0
        
        for name, output in active.items():
            w = self.weights.get(name, 1.0 / len(active))
            # Normalizar strength a [0, 1]
            normalized_score = min(1.0, abs(output.strength) / 6.0)
            
            if output.side == "BUY_YES":
                score_yes += normalized_score * w
            else:
                score_no += normalized_score * w
        
        # Detectar conflicto
        if abs(score_yes - score_no) < conflict_threshold:
            return None  # señales en conflicto; no operar
        
        side = "BUY_YES" if score_yes > score_no else "BUY_NO"
        composite_score = max(score_yes, score_no)
        
        contributing = [n for n, o in active.items() if o.side == side]
        conflicting = [n for n, o in active.items() if o.side != side]
        
        confidence = len(contributing) / len(active)
        
        return CompositeEdgeResult(
            side=side,
            composite_score=composite_score,
            edges_contributing=contributing,
            edges_conflicting=conflicting,
            confidence=confidence,
        )

    def calibrate_weights(
        self,
        backtest_results: dict[str, BacktestMetrics],
        min_ev: float = 0.0,
    ) -> None:
        """Actualizar pesos basados en EV histórico reciente."""
        new_weights = {}
        for name, metrics in backtest_results.items():
            if metrics.ev_per_signal > min_ev:
                new_weights[name] = max(0.0, metrics.ev_per_signal)
            else:
                new_weights[name] = 0.0  # edge sin EV+ no contribuye
        
        total = sum(new_weights.values())
        if total > 0:
            self.weights = {k: v / total for k, v in new_weights.items()}
        # Si total = 0: ningún edge validado → no usar composite
```

---

## Reglas de Amplificación y Penalización

| Situación | Modificación |
|---|---|
| ≥3 edges alineados | `composite_score × 1.25` (señal fuerte) |
| 1 edge contradice | `composite_score × 0.85` (penalizar conflicto) |
| Overreaction Y Momentum Exhaustion coinciden | `confidence += 0.15` (son complementarios) |
| Volume Anomaly contradice Overreaction | Rechazar (movimiento informado vs emocional es un conflicto serio) |

---

## Calibración de Pesos

Los pesos se calibran semanalmente mediante el job de reentrenamiento:

```python
# Cada domingo 02:00 UTC
async def recalibrate_composite_weights():
    results = {}
    for edge_name in ACTIVE_EDGES:
        metrics = await compute_backtest_metrics(
            edge_name=edge_name,
            lookback_days=30,
        )
        results[edge_name] = metrics
    
    engine = composite_engine_singleton
    engine.calibrate_weights(results, min_ev=0.001)
    
    # Persistir pesos en Redis para que el orchestrator los use
    await redis.set(
        "composite:weights",
        json.dumps(engine.weights),
        ex=7 * 24 * 3600,  # TTL 7 días
    )
    
    log.info("composite.weights_updated", weights=engine.weights)
```

---

## Threshold del Composite

```
Profit Factor objetivo: > 1.5
Umbral de composite_score: calibrar en grid search

Pseudocódigo de calibración:
  for threshold in [0.3, 0.4, 0.5, 0.6, 0.7]:
    for conflict_thresh in [0.10, 0.15, 0.20]:
      result = backtest_composite(threshold, conflict_thresh, data_out_of_sample)
      if result.profit_factor > 1.5 and result.max_dd < 0.10:
        candidates.append((threshold, conflict_thresh, result))
  
  # Elegir la combinación más conservadora (menor score, más robusto)
```

---

## Criterios de Activación del Composite

1. ≥ 2 edges individuales con Brier < 0.25 y EV+ en walk-forward.
2. Los edges activos deben tener correlación de señales < 0.80 entre sí (diversificación real).
3. El composite debe demostrar Profit Factor > 1.5 en backtest out-of-sample.
4. Activar primero con `composite_min_edges = 2`; aumentar a 3 cuando haya más edges validados.

---

## Tabla de Correlación entre Edges (objetivo: baja correlación)

| | E01 | E03 | E06 | E07 | E08 | E09 |
|---|---|---|---|---|---|---|
| **E01 Overreaction** | — | Baja | Baja | Alta (conflicto) | Media | Alta (complementario) |
| **E03 Market Structure** | Baja | — | Baja | Baja | Baja | Baja |
| **E06 Liquidity Vacuum** | Baja | Baja | — | Media | Alta | Baja |
| **E07 Volume Anomaly** | Alta (conflicto) | Baja | Media | — | Media | Baja |
| **E08 Spread Expansion** | Media | Baja | Alta | Media | — | Baja |
| **E09 Momentum Exhaustion** | Alta (complementario) | Baja | Baja | Baja | Baja | — |

*Alta conflicto*: pueden contradecirse (E01 + E07 son el caso más conocido).
*Alta complementario*: suelen coincidir y amplificar la señal.

---

## Pre-requisitos de Implementación

1. Mínimo 2 edges individuales con backtest aprobado.
2. Tabla `backtest_edge_metrics` en Postgres para persistir resultados semanales.
3. Job de recalibración semanal (Alembic migration + nuevo background task).
4. Tests: mock de 3 edges (2 alineados, 1 en conflicto) → señal correcta con penalización.
