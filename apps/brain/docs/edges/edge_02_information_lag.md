# Edge 02 — Information Lag

> **Estado**: NO IMPLEMENTADO. Requiere ETL externo (Bloque B).

---

## Hipótesis

Polymarket no reacciona instantáneamente a toda información. Fuentes como APIs gubernamentales, filings regulatorios, PDFs técnicos, datos de mercados internacionales y anuncios en idiomas no-ingleses llegan antes al análisis manual que al precio del mercado. No hay que ser más rápido que un HFT, solo más rápido que el humano promedio que tarda 15-60 minutos en procesar y reaccionar.

El edge: detectar que existe información directamente relevante que **todavía no está reflejada** en el precio del mercado.

---

## Señal Matemática

```
T_event   = timestamp del evento externo (RSS, API gubernamental, news feed)
T_move    = timestamp de primer movimiento significativo en Polymarket (|delta_p_5m| > θ_move)
Lag       = T_move - T_event

Si Lag > θ₂ minutos Y sentiment(evento) es relevante:
  → señal en dirección del evento (BUY_YES si evento positivo para YES, BUY_NO si negativo)

Condición adicional de validez:
  |mid_velocity| < threshold (precio aún no se movió significativamente)
```

---

## Features Requeridos

| Feature | Fuente | Requiere |
|---|---|---|
| `event_timestamp` | ETL externo (RSS, GDELT, NewsAPI) | Pipeline ETL (GAP-04) |
| `event_sentiment` | NLP sobre título/descripción del evento | Modelo de sentimiento |
| `event_relevance_score` | Similitud semántica con el mercado | Word embeddings o keyword matching |
| `mid_velocity` | `features/calculator.py` | Ya implementado |
| `delta_p_5m` | `features/calculator.py` | Ya implementado |
| `mentions_per_hour` | ETL externo (Twitter/RSS) | Pipeline ETL |

---

## Parámetros Configurables

| Parámetro | Valor sugerido | Descripción |
|---|---|---|
| `lag_min_minutes` | 15 | Lag mínimo para considerar que el mercado no reaccionó |
| `lag_max_minutes` | 120 | Lag máximo (si pasó más de 2h, el mercado probablemente ya reaccionó) |
| `relevance_min_score` | 0.60 | Score mínimo de relevancia evento-mercado |
| `velocity_max` | 0.001 | Si velocity > este valor, el mercado ya está moviendo |

---

## Pseudocódigo

```python
def detect_information_lag(
    condition_id: str,
    as_of: datetime,
    external_events: list[ExternalEvent],
    features: FeatureSet,
    min_lag_min: float = 15.0,
    max_lag_min: float = 120.0,
    min_relevance: float = 0.60,
) -> EdgeOutput | None:
    
    # Solo aplicar si el precio aún no se movió
    if features.mid_velocity is not None and abs(features.mid_velocity) > 0.001:
        return None
    
    # Buscar eventos recientes relevantes que el mercado no haya incorporado
    for event in external_events:
        lag_min = (as_of - event.timestamp).total_seconds() / 60
        
        if not (min_lag_min <= lag_min <= max_lag_min):
            continue
        
        if event.relevance_score < min_relevance:
            continue
        
        # Dirección: el evento ¿es bullish o bearish para YES?
        side = "BUY_YES" if event.sentiment > 0 else "BUY_NO"
        lag_score = 1.0 - (lag_min / max_lag_min)  # más score cuanto más fresco el lag
        
        return EdgeOutput(
            edge_name="information_lag",
            side=side,
            market_price=features.mid_price,
            fair_price=None,  # no conocemos el fair; confiamos en la dirección del evento
            edge_value=event.sentiment * lag_score,
            strength=lag_score,
            reason=f"lag={lag_min:.0f}min source={event.source}",
        )
    
    return None
```

---

## Pipeline ETL Requerido

```
Scheduler (cada 5 min):
  1. Fetch RSS feeds configurados (lista en config: rss_feeds)
  2. Para cada ítem nuevo:
     a. Extraer keywords del título
     b. Buscar condition_ids relacionados (por keyword matching sobre market.question)
     c. Calcular sentiment (VADER o similar, offline, sin API)
     d. INSERT INTO external_events(condition_id, source, event_ts, sentiment, summary)
     e. Redis SET: ext:{condition_id}:latest (TTL 2h)
  3. Fetch GDELT API (top events por país/tema cada hora)
```

**Modelo de sentimiento recomendado**: `transformers` con modelo multilingüe (BETO para español, FinBERT para finanzas en inglés). Si los recursos son limitados, VADER es suficiente para titulares en inglés.

---

## Criterios de Validación

| Métrica | Umbral |
|---|---|
| Brier score | < 0.25 |
| EV por señal | > 0.003 |
| Lag > 0 en test (sin cheating temporal) | Verificado con test anti-lookahead |
| Precision@10 de relevance_score | > 70% |

**Riesgo principal**: el relevance matching es difícil. Un falso positivo (evento irrelevante marcado como relevante) destruye el EV. Empezar con keyword matching simple antes de embeddings.

---

## Pre-requisitos de Implementación

1. GAP-04 resuelto: pipeline ETL externo activo.
2. Tabla `external_events` creada (migración Alembic).
3. Caché Redis `ext:{condition_id}:latest` poblado.
4. Tests anti-lookahead: event_timestamp debe ser estrictamente < as_of.
