# umbraNocti — Roadmap

Documento vivo. Alineado a la filosofía documentada en `STRATEGY.md`.

## Dónde estamos

**Mes 1 (Infraestructura) — COMPLETADO** en 5 días (D1-D5):

- Pipeline real Polymarket → snapshots → features → edge → risk → señal → paper fill → posición.
- Edge 1 (Overreaction) implementado y testeado contra DB real.
- 28 tests verdes (incluyendo anti-lookahead, E2E, paper execution).
- Dashboard Streamlit con KPIs, equity curve, kill-switch.
- Documentación: `STRATEGY.md`, `DOCUMENTATION.md`, `OPERATIONS.md`.

**Siguiente fase**: Mes 2 (Exploración + Validación). Este documento detalla las próximas **4 semanas** (~30 días) que componen el Mes 2 completo.

---

## Principios obligatorios del plan

De `STRATEGY.md`:

1. **NO agregar Edges 2/3/4/5 (Late Info, Market Structure, Narrative Decay, Event Vol)** hasta validar Overreaction. La filosofía dice claro: solo Overreaction + Mean Reversion al principio.
2. **NO optimizar más de 2-3 parámetros.** Si el edge depende de un valor exacto, no es edge.
3. **NO usar IA compleja** (ML, RL, LLM) hasta tener edge base validado.
4. **NO operar real.** Mínimo 60-90 días de paper.
5. **Supervivencia > retorno.** DD > 10% reduce tamaño, DD > 15% pausa.
6. **Disciplina sobre genialidad.** El edge real es no engañarse a uno mismo.

**Meta del mes**: ¿el edge Overreaction (eventualmente + Mean Reversion) tiene EV+ real sobre datos verificados? Si sí → seguir paper trading hasta Mes 4-6 según roadmap. Si no → pivotar a otro edge sin emociones.

---

# Plan detallado — 4 semanas / ~30 días

## 🗓️ Semana 1 — Validación honesta del edge actual

**Objetivo**: ¿Overreaction tiene EV+ en datos reales? Brier real, walk-forward, sensibilidad.

### Día 1-2 — Acumulación pasiva

- Sistema corriendo continuo 48-72 horas (`python scripts/run_api.py`).
- **Cero código.** Resistir la tentación de "ajustar" antes de tener evidencia.
- Meta: > 5,000 snapshots, > 50 mercados únicos en `markets`.
- Apuntar en libreta: cuántas señales se evaluaron, cuántas se aceptaron, cuál fue el primer aceptado.

### Día 3 — Outcomes resolver

- Tabla nueva `outcomes` (market_id, resolved_at, outcome_yes 0/1, source).
- Job async cada 1 h: consulta Gamma para mercados con `endDate` pasado, los marca como resueltos.
- Backfill: resolver retroactivamente todos los mercados ya cerrados que tenemos en `markets`.

### Día 4 — Métricas reales en dashboard

- Reemplazar los KPIs "N/A" con números honestos:
  - **Brier score**: `mean((p_fair - outcome)^2)` sobre señales con outcome conocido.
  - **Hit rate paper**: % de señales aceptadas cuyo lado coincidió con outcome.
  - **EV+**: PnL realizado promedio por señal.
  - **Sharpe paper**: sobre serie diaria de retornos.

### Día 5 — Backtester offline

- Script `infra/backtest.py`: `as_of` deslizante cada 5 min sobre snapshots históricos.
- Replay del orchestrator con cada timestamp.
- CSV con todas las señales que se habrían generado.
- Verificar contra outcomes resueltos.

### Día 6 — Walk-forward simple

- Histórico dividido temporalmente: train (primer 60%) y test (último 40%).
- Métricas separadas para cada bucket.
- **Criterio**: si strength del edge cae > 30% entre train y test → no hay edge real → pivotar.

### Día 7 — Sensibilidad de parámetros (límite duro: 2)

- Variar `OVERREACTION_SIGMA_THRESHOLD` en {2.5, 3.0, 3.5, 4.0}.
- Variar `EMA_ALPHA` en {0.05, 0.10, 0.15, 0.20}.
- Si el resultado cambia dramáticamente con cualquier ajuste → no es robusto.
- **Compromiso**: solo estos 2 parámetros. No tocar otros para "mejorar números".

**Entregable Semana 1**: `FINDINGS_W1.md` con tabla de métricas + decisión preliminar.

---

## 🗓️ Semana 2 — Selección de mercados según filosofía

**Objetivo**: aplicar la Capa 1 del Umbra PMM v1 (priorizar nichos, evitar saturados).

### Día 8 — Filtros de mercados ideales

Modificar `universe/scanner.py`:
- **Excluir**: liquidez > $50,000 (saturado), liquidez < $5,000 (slippage mata edge), spreads < $0.005 (HFT-dominated).
- **Priorizar** (rank top): liquidez $5,000-$30,000 (zona "media"), volumen 24h / liquidez alto (mucha actividad emocional vs base estable).
- Logging detallado: por qué cada mercado fue incluido/excluido.

### Día 9 — Filtros temporales

- ¿Hay horas donde Overreaction funciona mejor (UTC)?
- ¿Cerca del `endDate` el edge se rompe (información perfecta = menos overreaction)?
- Agrupar señales históricas por bucket horario + bucket de tiempo a expiración.
- Si hay diferencias claras → agregar filtros (ej: no operar últimas 6 h antes de expiración).

### Día 10 — Categorización de mercados

- Etiquetar mercados por categoría: política presidencial / política secundaria / deportes / cripto / cultura.
- Métricas por categoría sobre datos reales: ¿en cuáles el edge brilla?
- Según filosofía: deberían brillar en **secundarios con narrativa emocional**, no en presidenciales ultra-eficientes.

### Día 11 — Refinamiento basado en evidencia

- Aplicar filtros que la Semana 1-2 mostraron como ganadores.
- Re-correr backtest con filtros nuevos.
- Comparar métricas: filtros vs sin filtros.
- Si los filtros no mejoran nada → quitarlos. NO añadir complejidad sin evidencia.

### Día 12 — Tests de regresión

- Asegurarnos que los cambios en filtros NO rompen tests existentes.
- Agregar tests para los nuevos filtros.
- Total esperado: ~35 tests verdes.

### Día 13 — Stress de slippage

- Verificar que el edge sobrevive al modelo de slippage actual.
- Probar con slippage más conservador: ¿qué pasa si subimos `slippage_base_bps` a 50?
- Si el edge desaparece con slippage realista → no es edge (regla 3 de STRATEGY.md).

### Día 14 — Checkpoint estratégico

- Decisión basada en evidencia de las primeras 2 semanas:
  - **Edge validado** (Brier < 0.20, EV+ > 0, robusto a parámetros y slippage): seguir a Semana 3 con plan Mean Reversion.
  - **Edge incierto**: extender Semana 1-2 otras 2 semanas con más datos antes de agregar Mean Reversion.
  - **Edge no validado**: pivotar a Edge 3 (Market Structure) que es más matemático y menos dependiente de narrativa.

**Entregable Semana 2**: `FINDINGS_W2.md` y decisión documentada de continuar/pausar/pivotar.

---

## 🗓️ Semana 3 — Mean Reversion como segundo edge

**Pre-requisito**: Semana 2 cerró con "Edge validado". De lo contrario, esta semana se reemplaza por más validación.

### Día 15-16 — Implementar Mean Reversion edge

- `src/umbra/edges/mean_reversion.py`:
  - Detecta cuando `mid_price` cruza hacia su EMA (reversión en curso).
  - Genera señal **independiente** de Overreaction.
  - Threshold: el cruce debe ser > 1.5σ del ruido base reciente.
- Tests anti-lookahead obligatorios.

### Día 17 — Integrar al orchestrator

- Orchestrator ahora evalúa AMBOS edges en cada tick.
- Cada uno persiste su propia señal con su `edge_name`.
- El risk engine sigue siendo el mismo (rechaza si MIN_EDGE, MAX_RISK, etc.).

### Día 18 — Combinador básico

- Si Overreaction dice BUY_NO y Mean Reversion dice BUY_YES en el mismo mercado: **rechazar ambas** (conflicto = incertidumbre).
- Si ambos dicen lo mismo: aceptar con `notional` 1.5× (más confianza).
- Si solo uno dice algo: tratar normal.

### Día 19 — Análisis de ortogonalidad

- ¿Cuánto se solapan las señales de los 2 edges?
- Correlación de strength entre ambos en mercados comunes.
- Si están 100% correlacionados → no agregan diversificación → eliminar Mean Reversion.
- Si están ortogonales → buena cosa, diversificación real.

### Día 20 — Edge attribution en dashboard

- Tabla nueva en dashboard: PnL paper agrupado por `edge_name`.
- KPI: contribución relativa de cada edge.
- Detectar si un edge es "carrier" y el otro es "ruido".

### Día 21 — Documentar Semana 3

- `FINDINGS_W3.md` con resultados de ortogonalidad y métricas por edge.
- Updated `STRATEGY.md` si hay aprendizajes que ajustan la filosofía.

---

## 🗓️ Semana 4 — Robustez, gestión de riesgo automática, decisión

**Objetivo**: implementar las reglas de DD y kill-switch automáticas de STRATEGY.md, y tomar decisión informada sobre Mes 3.

### Día 22 — Portfolio Kelly (no per-trade)

- Refactor del sizer: considerar exposure total al portfolio.
- Regla: riesgo total simultáneo nunca supera 10% del bankroll (consistente con regla 1-2% por trade × ~10 trades concurrentes).
- Si una nueva señal pondría el riesgo total > 10% → recortar o rechazar.

### Día 23 — Circuit breakers automáticos

- Background task cada 5 min calcula DD actual del portfolio paper.
- **DD > 10%**: reducir `kelly_kappa` a la mitad automáticamente, log warning, exponer en `/portfolio/health`.
- **DD > 15%**: activar `umbra:halt` automáticamente, log error, notificar en dashboard.
- Endpoint `/portfolio/health` con DD actual y estado de circuit breakers.

### Día 24 — Slippage refinado

- Validar el modelo simple actual contra fills reales (cuando los empecemos a ver más).
- Si el slippage real es muy diferente del estimado, ajustar el modelo.
- Posiblemente switch a modelo proporcional a (notional)² para tamaños grandes.

### Día 25 — Detección de comportamiento fuera de distribución

- Implementar regla "kill-switch si comportamiento fuera de distribución" de STRATEGY.md.
- Métricas a monitorear:
  - Strength de las señales (¿de repente todas son > 10σ?)
  - Velocidad de movimientos de precio (¿el mercado cambió de régimen?)
  - Volume z-score promedio (¿spike masivo?)
- Si ≥ 2 métricas salen 3σ fuera de su distribución histórica → trigger del kill-switch.

### Día 26 — Stress tests

- Test E2E que simula un shock de mercado (mid_price salta 50% en un tick).
- Verificar que: el sistema NO entra en pánico, el circuit breaker activa correctamente, el portfolio no se va a 0.
- Documentar el comportamiento.

### Día 27 — FINDINGS finales del Mes 2

- Crear `FINDINGS_M2.md` con:
  - Tabla resumen: N señales total/aceptadas/Brier/winrate/EV+/Sharpe/maxDD para Overreaction, Mean Reversion, y combinado.
  - Resultado de walk-forward.
  - Resultado de sensibilidad de parámetros.
  - Lista de mercados donde funcionó bien / mal.
  - Tiempo total operando, tiempo en halt, número de circuit breaker activations.

### Día 28 — Decisión estratégica

Tres caminos, decidir basados en `FINDINGS_M2.md`:

#### Camino A — Sistema validado
**Criterios** (todos deben cumplirse):
- Brier sostenido < 0.20.
- EV+ por señal positivo después de slippage.
- Walk-forward sin degradación > 30%.
- Sensibilidad: edge sobrevive ±20% en cada parámetro.
- Max DD paper < 15%.

**Acción**:
- Seguir paper trading 60-90 días más (Mes 3-5) sin agregar features.
- Empezar planificación de Edge 3 (Market Structure) para Mes 4.
- Empezar planificación de integración Twitter/sentiment para Mes 5.

#### Camino B — Sistema parcial
**Criterios**: algunas métricas buenas, otras malas. Brier ~0.22, EV+ marginal, walk-forward degrada 30-50%.

**Acción**:
- Continuar paper trading otro mes con sistema actual, sin tocar nada.
- Si en Mes 3 las métricas mejoran (más datos → menos varianza) → seguir.
- Si empeoran → pivotar.

#### Camino C — Sistema no funciona
**Criterios**: Brier > 0.25, EV+ negativo, walk-forward degrada > 50%, parámetros frágiles.

**Acción**:
- Documentar en `FINDINGS_M2.md` por qué no funcionó. Lo aprendido vale.
- Pivotar a Edge 3 (Market Structure — incoherencias matemáticas, no narrativa). Es más cuantitativo y menos dependiente del retail.
- Re-leer `STRATEGY.md` antes de empezar para no caer en sobreoptimización.

### Día 29-30 — Buffer + actualizar docs

- Días de buffer para tareas atrasadas (especialmente backfill de outcomes si tomó más tiempo).
- Actualizar `ROADMAP.md` con el nuevo plan del Mes 3 según la decisión del Día 28.
- Si Camino A: el Mes 3 es "más paper trading + Twitter sentiment". Plan detallado.
- Si Camino C: el Mes 3 es "Edge 3 Market Structure". Plan detallado.

**Entregable Mes 2**: `FINDINGS_M2.md` + ROADMAP del Mes 3.

---

## Lo que explícitamente NO haremos en estas 4 semanas

Siguiendo la filosofía STRATEGY.md:

- ❌ **NO** agregar Edges 3/4/5 todavía. Solo Overreaction + Mean Reversion.
- ❌ **NO** integrar Twitter/X/sentiment en este mes. Es Mes 3 según roadmap.
- ❌ **NO** introducir ML/IA (regresión, árboles, bayes). Solo modelos simples.
- ❌ **NO** tocar dinero real. Mínimo 60-90 días de paper antes.
- ❌ **NO** migrar a WebSocket. Polling REST 30s sigue suficiente.
- ❌ **NO** containerizar / VPS. Local OK hasta validar edge.
- ❌ **NO** optimizar más de 2 parámetros del edge. Regla 1.
- ❌ **NO** aumentar bankroll por confianza emocional. Regla 5.

---

## Después del Mes 2 (vista panorámica)

Si Camino A (validado):

- **Mes 3**: Paper trading estricto continuado + Twitter sentiment como **input al edge** (no edge nuevo).
- **Mes 4-5**: Paper trading 60-90 días. Documentar todo. Cero cambios al edge sin justificación estadística.
- **Mes 6**: Edge 3 (Market Structure) si las métricas mantienen salud. Reconciliación on-chain inicia.
- **Mes 7-12**: Camino a dinero real con criterios duros (ver sección final).

Si Camino C (pivotar):

- **Mes 3**: implementar Edge 3 (Market Structure) desde cero. Pipeline diferente.
- **Mes 4-6**: validar Edge 3 con misma rigurosidad.
- **Mes 7+**: re-evaluar.

---

## Criterios NO negociables para tocar dinero real (Mes 7+ en escenario optimista)

1. ≥ 90 días de paper trading continuo con outcomes resueltos.
2. Brier score sostenido < 0.20.
3. Sharpe paper > 1.0 después de slippage modelado.
4. Walk-forward sin degradación > 20% entre train y test.
5. Max drawdown paper < 15%.
6. Circuit breakers activos funcionando.
7. Sensibilidad de parámetros: edge sobrevive ±20% en cada hiperparámetro.
8. `FINDINGS_*.md` documentado mes a mes.
9. Reconciliación on-chain implementada y testeada.
10. Alertas configuradas (Telegram o equivalente).

**Si UNA falla → no se opera con dinero. Sin excepción.**
