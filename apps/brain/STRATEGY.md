# Umbra PMM v1 — Filosofía y estrategia

> Documento maestro de principios. Toda decisión técnica del proyecto debe poder justificarse contra este documento. Si no se alinea, no se hace.

---

## Objetivo real del sistema

Construir un sistema estadístico y semi-automatizado capaz de:

- Detectar ineficiencias probabilísticas en mercados de Polymarket.
- Operar sin emociones.
- Sobrevivir a cambios de régimen.
- Mantener riesgo controlado.
- Priorizar consistencia antes que retornos explosivos.
- Crear pipeline constante de descubrimiento de edges.

**NO buscamos**:
- "hacerse rico rápido"
- all-in emocionales
- predicción perfecta
- IA mágica
- martingalas
- señales Twitter copy-trade sin validación

**Prioridades (en orden)**:
1. supervivencia
2. disciplina
3. repetibilidad
4. ventaja estadística
5. escalabilidad

---

## Filosofía base

### Polymarket NO es apuestas deportivas

Polymarket es:
- microestructura de mercado
- información asimétrica
- velocidad de reacción
- sesgos cognitivos humanos
- liquidez imperfecta
- comportamiento colectivo

**El edge NO está en "adivinar el futuro".** Está en:
- probabilidades mal calibradas
- sobre-reacción
- retrasos informacionales
- narrativa emocional
- liquidez desigual
- pricing incorrecto temporal

### Realidad estructural

Después del boom electoral:
- mercados obvios están arbitrados
- traders sofisticados entraron
- bots ya explotan spreads simples
- scraping básico ya no basta

**NO competirás**:
- por velocidad pura
- por capital institucional
- por HFT

**Debes competir**:
- en nichos
- en interpretación
- en modelado probabilístico
- en timing emocional
- en selección de mercados

---

## Arquitectura estratégica

### Capa 1 — Selección de mercados

**Priorizar**:
- mercados políticos secundarios
- eventos con narrativa emocional
- mercados con liquidez media
- mercados con mala cobertura informativa
- mercados donde Twitter/X influya fuerte
- mercados donde retail reaccione emocionalmente

**Evitar**:
- mercados ultra principales
- elecciones centrales hiper-eficientes
- mercados con MM institucional dominante
- mercados con spreads mínimos

### Capa 2 — Detección de edge

#### Edge 1 — Overreaction edge

**Idea**: el retail sobre-reacciona a noticias, compra pánico, compra euforia, ignora probabilidad base. Esto crea spikes irracionales, desviaciones temporales, reversión parcial.

**Estrategia**: detectar cambios rápidos > X%, aumento súbito de volumen, correlación con noticia reciente, sentimiento extremo. Entrar contra el movimiento emocional, solo si el modelo indica sobre-extensión.

**Ejemplo**: "Candidato X ganará debate" pasa de 42% → 67% en 12 minutos. Twitter explota. Modelo estima valor justo = 54%. Edge: short / reversión.

#### Edge 2 — Late information edge

**Idea**: Polymarket NO reacciona instantáneamente a toda información. Especialmente noticias locales, fuentes técnicas, datos regulatorios, mercados internacionales, documentos PDF, filings, APIs oficiales.

**Sistema**: pipelines de RSS, Twitter/X, scraping de medios, alertas regulatorias, APIs públicas, scraping de debates/transcripciones. Detectar información antes de que el retail la procese. **No hay que ser más rápido que HFT, solo más rápido que el humano promedio.**

#### Edge 3 — Market structure edge

**Idea**: muchos mercados tienen mala liquidez, spreads absurdos, pricing inconsistente, relaciones matemáticas incorrectas.

**Ejemplo**: si A = 70%, B = 40%, pero son mutuamente excluyentes, hay incoherencia y por tanto edge.

#### Edge 4 — Narrative decay edge

**Idea**: las narrativas emocionales mueren. El retail sobreestima el impacto inmediato, extrapola noticias recientes, ignora la memoria social corta.

**Ejemplo**: escándalo político hace colapsar un mercado. 48h después el interés social desaparece. La probabilidad rebota.

#### Edge 5 — Event volatility edge

**Idea**: antes de eventos (debates, CPI, Fed, juicios, entrevistas, earnings políticos), la volatilidad implícita emocional aumenta. El mercado frecuentemente sobre-hedgea y exagera probabilidades extremas.

---

## Fases del sistema

### Fase 1 — Data engine

**Datos mínimos** (Polymarket): precio histórico, order book, volumen, spreads, timestamps, cambios abruptos, liquidez.

**Datos externos**: Twitter/X, Google Trends, noticias, Reddit, RSS, APIs políticas, encuestas, eventos macro.

### Fase 2 — Feature engineering

- **Momentum**: variación 5m, variación 15m, aceleración
- **Liquidez**: spread, profundidad, slippage estimado
- **Social**: sentimiento, menciones/minuto, velocidad narrativa
- **Estadísticas**: z-score, desviación histórica, reversión media, volatilidad

### Fase 3 — Modelo

**NO empezar con IA compleja**. Error típico: multiagentes, transformers, RL, LLMs traders **antes de validar edge simple**.

**Primero**:
- regresión logística
- modelos bayesianos
- árboles simples
- thresholds estadísticos
- mean reversion

La IA avanzada solo entra cuando exista edge base validado.

### Fase 4 — Validación

**Regla crítica**: un backtest bonito NO significa edge real.

Validar:
- out-of-sample
- walk-forward
- paper trading
- robustness
- cambios de régimen

---

## Reglas antifraude cognitivo

1. **Nunca optimizar más de 2-3 parámetros**.
2. **Si una estrategia deja de funcionar al cambiar ligeramente parámetros**: no hay edge.
3. **Si el edge depende de timing perfecto, ejecución perfecta o cero slippage**: no es real.
4. **Paper trade mínimo: 60-90 días** antes de tocar capital real.
5. **Nunca aumentar tamaño por confianza emocional**. Solo por evidencia estadística, número de trades, estabilidad.

---

## Gestión de riesgo

**Regla central**: la supervivencia importa más que el retorno.

**Position sizing**: 1-2% por trade máximo. Nunca 10%, nunca 20%, nunca 50%, nunca all-in.

**Drawdown limits**:
- DD > 10%: reducir tamaño.
- DD > 15%: pausa. Revisión completa.

**Kill switch**: apagar sistema si edge desaparece, mercado cambia estructura, slippage aumenta demasiado, volatilidad extrema, comportamiento fuera de distribución.

---

## Meta-edge real

El verdadero edge NO será "un modelo mágico". Será:

- disciplina
- validación rigurosa
- velocidad de iteración
- evitar autoengaño
- sobrevivir más tiempo que otros traders

---

## Roadmap realista (visión larga)

- **Mes 1**: infraestructura. Scraper Polymarket, base de datos, ingestión eventos, tracking. **NO operar fuerte**. *[En este proyecto: completado en 5 días — ver Día 1-5]*
- **Mes 2**: exploración. Buscar anomalías, overreactions, mercados ilíquidos, patrones repetitivos. *[En curso, ver `ROADMAP.md`]*
- **Mes 3**: primer modelo simple. Ejemplo: Mean Reversion + Twitter Sentiment.
- **Mes 4-6**: paper trading serio. Documentar winrate, EV, Sharpe, drawdown, estabilidad.
- **Mes 6-12**: validar primer edge real. Si no existe → pivot. Si existe → escalar lentamente.

---

## Stack técnico recomendado

- **Backend**: Python, FastAPI, PostgreSQL, Redis *(ya en uso ✅)*
- **Data**: Pandas, Polars, NumPy, DuckDB *(pandas en uso, otros pendientes)*
- **Quant**: scikit-learn, statsmodels, PyMC *(pendiente)*
- **Infra**: Docker, VPS Linux, cronjobs, queues *(pendiente — hoy corre local)*
- **Visualización**: Grafana, Streamlit *(Streamlit en uso ✅)*

---

## Métricas que importan

**NO obsesionarse con winrate.**

Importa más:
- EV+ (expected value positivo)
- Sharpe ratio
- estabilidad
- drawdown controlado
- consistencia

---

## Fórmula mental clave

> Muchos trades pequeños + ventaja pequeña + disciplina extrema > pocas apuestas gigantes emocionales.

---

## El mayor enemigo

NO será el mercado. Será:

- sobreoptimización
- ego
- impaciencia
- FOMO
- cambiar estrategia cada semana
- aumentar riesgo tras ganar
- intentar recuperar pérdidas

---

## Estrategia recomendada inicial

La mejor apuesta inicial **NO es hacer 10 sistemas**. Hacer SOLO:

# OVERREACTION + MEAN REVERSION

Validar primero esta combinación con disciplina antes de agregar Edges 2-5.
