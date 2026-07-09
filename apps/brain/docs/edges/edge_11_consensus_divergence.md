# Edge 11 — Consensus Divergence (Divergencia del Consenso)

> **Estado**: NO IMPLEMENTADO. Requiere APIs de encuestas y fuentes de consenso externas.

---

## Hipótesis

Los mercados de predicción agregan información de forma imperfecta. En particular, en mercados políticos y macroeconómicos, existe un consenso externo verificable (encuestas electorales, pronósticos de analistas, mercados alternativos como PredictIt o Metaculus) que a veces diverge significativamente de Polymarket.

Cuando la divergencia es mayor que el error estadístico esperado, el mercado de Polymarket tenderá a converger hacia el consenso externo, especialmente cuando el evento se acerca.

---

## Señal Matemática

```
P_market     = mid_price en Polymarket
P_external   = probabilidad de consenso externo (media ponderada de fuentes)

Divergence = P_market - P_external
Divergence_z = Divergence / σ_histórica_de_divergencias

Si |Divergence| > θ₁₁ (ej. 0.08, 8 puntos porcentuales):
  Y la fuente externa tiene credibilidad alta:
    → Si P_market > P_external: BUY_NO (Polymarket sobre-valuado vs consenso)
    → Si P_market < P_external: BUY_YES (Polymarket infra-valuado vs consenso)

Confirmación: divergencia debe ser sostenida > 48h (no transitoria)
```

---

## Features Requeridos

| Feature | Fuente | Requiere |
|---|---|---|
| `poll_probability` | APIs de encuestas (Metaculus, PredictIt, FiveThirtyEight) | ETL externo |
| `poll_source_count` | Cuántas fuentes tienen dato para este mercado | ETL externo |
| `poll_age_hours` | Antigüedad del dato externo más reciente | ETL externo |
| `divergence_history` | Serie histórica de divergencias | DuckDB |
| `time_to_resolution_h` | Horas hasta resolución | Ya disponible |

---

## Fuentes Externas y Credibilidad

| Fuente | Tipo | Credibilidad |
|---|---|---|
| Metaculus | Pronósticos de expertos | Alta |
| FiveThirtyEight (archivos públicos) | Modelos estadísticos | Alta |
| PredictIt | Mercado alternativo | Media |
| Encuestas electorales (Real Clear Politics) | Agregador de encuestas | Media |
| Polymarket Gamma (otros contratos relacionados) | Mercado interno | Alta (para arbitraje) |
| Reddit/Twitter polls | Encuestas informales | Baja (no usar) |

---

## Parámetros Configurables

| Parámetro | Valor sugerido | Descripción |
|---|---|---|
| `divergence_threshold` | 0.08 | 8pp de diferencia mínima |
| `consensus_min_sources` | 2 | Mínimo 2 fuentes para confiar en el consenso |
| `consensus_max_age_hours` | 48 | Datos de consenso no más viejos de 48h |
| `divergence_min_days` | 1 | Divergencia sostenida al menos 1 día |
| `min_tte_hours` | 24 | No operar si queda <24h para resolución |

---

## Pseudocódigo

```python
@dataclass
class ExternalConsensus:
    probability: float          # 0..1
    sources: list[str]
    latest_update: datetime
    confidence: float           # 0..1 (basado en calidad/cantidad de fuentes)

async def load_consensus(
    condition_id: str,
    as_of: datetime,
    redis_client,
) -> ExternalConsensus | None:
    """Lee del cache Redis el consenso externo para este mercado."""
    raw = await redis_client.get(f"consensus:{condition_id}")
    if raw is None:
        return None
    data = json.loads(raw)
    return ExternalConsensus(**data)


def detect_consensus_divergence(
    features: FeatureSet,
    consensus: ExternalConsensus,
    time_to_resolution_h: float,
    as_of: datetime,
    divergence_threshold: float = 0.08,
    min_sources: int = 2,
    max_age_hours: float = 48.0,
    min_tte_hours: float = 24.0,
) -> EdgeOutput | None:
    
    if time_to_resolution_h < min_tte_hours:
        return None
    
    if len(consensus.sources) < min_sources:
        return None  # consenso poco respaldado
    
    consensus_age_h = (as_of - consensus.latest_update).total_seconds() / 3600
    if consensus_age_h > max_age_hours:
        return None  # datos obsoletos
    
    if features.mid_price is None:
        return None
    
    divergence = features.mid_price - consensus.probability
    
    if abs(divergence) < divergence_threshold:
        return None
    
    # El edge es proporcional a la credibilidad del consenso
    effective_edge = abs(divergence) * consensus.confidence
    
    side = "BUY_NO" if divergence > 0 else "BUY_YES"
    
    return EdgeOutput(
        edge_name="consensus_divergence",
        side=side,
        market_price=features.mid_price,
        fair_price=consensus.probability,
        edge_value=abs(divergence),
        strength=effective_edge,
        reason=f"polymarket={features.mid_price:.4f} consensus={consensus.probability:.4f} "
               f"sources={len(consensus.sources)} age={consensus_age_h:.1f}h",
    )
```

---

## Pipeline ETL de Consenso

```
Scheduler (cada 6h):
  1. Para cada mercado activo en markets_active:
     a. Extraer keywords del market.question
     b. Query a Metaculus API: buscar preguntas similares por keywords
     c. Query a archivos públicos FiveThirtyEight (donde aplica)
     d. Si mercado es en PredictIt: scrapear probability pública
     e. Calcular media ponderada por credibilidad de fuente
     f. Redis SET consensus:{condition_id} (TTL 8h)
  2. Log de cobertura: cuántos mercados tienen consenso externo disponible
```

---

## Criterios de Validación

| Métrica | Umbral |
|---|---|
| EV por señal | > 0.008 (este edge tiene mayor horizonte temporal → mayor EV esperado) |
| Brier score vs consenso | El fair price debe ser mejor predictor que el mercado en >60% de los casos |
| Cobertura del universo | Consenso disponible para > 30% del top-20 |
| Tiempo promedio de convergencia | < 7 días |

**Riesgo principal**: el consenso externo puede estar sistemáticamente sesgado (p.ej. encuestas de medios pro-partido). Usar solo fuentes con track record histórico verificado. Metaculus y FiveThirtyEight son las fuentes de mayor confianza disponibles.

**Nota**: este es el edge de más largo horizonte temporal del sistema. No generar señales de menos de 24h de TTL con este edge. Configurar position_ttl_hours específico para este edge más largo que el default de 8h.

---

## Pre-requisitos de Implementación

1. Tabla `consensus_external` en Postgres: (condition_id, source, probability, confidence, updated_at).
2. Pipeline ETL de consenso corriendo cada 6h.
3. Cache Redis `consensus:{condition_id}` con TTL 8h.
4. Tests: mock de consenso divergente → señal correcta generada.
