# Los 12 edges — índice y criterio común

Cada `edge_NN_*.md` es la especificación de un edge: hipótesis, señal matemática,
pseudocódigo y métricas de aceptación. **Solo el 01 (Overreaction) está implementado**;
el 09 (Momentum Exhaustion) tiene una versión reducida en `edges/momentum.py`. El resto
son diseño sin código, y el orden de implementación lo fija `ROADMAP.md`, no este
directorio.

## ⚠️ El umbral de Brier de cada ficha está obsoleto

Las tablas de métricas de estos documentos piden cosas como «Brier score < 0.25» o
«< 0.22». **Ese criterio se retiró el 28/07/2026 y no debe usarse para aceptar ningún
edge.**

El motivo, medido sobre 1.799 eventos reales del propio sistema:

| | Brier |
|---|---|
| modelo | 0.0723 |
| **precio de mercado** | **0.0722** |

Los dos aprueban un umbral de 0.25 con enorme holgura, y la diferencia entre ellos es
cero. Si la mayoría de mercados resuelven cerca de su precio, copiar el precio ya saca
un Brier bajo: **un umbral absoluto no premia acertar, premia operar donde es fácil
acertar.**

El criterio vigente para cualquier edge es **batir al precio de mercado en Brier con
significancia estadística**: el intervalo de confianza del 95% de la diferencia
emparejada, entero por debajo de cero, sobre ≥ 200 predicciones. Lo implementa
`backtest/metrics.brier_skill` y lo evalúa `MetricsReport.passes_acceptance`.

Fuente de verdad: [`ROADMAP.md`](../../ROADMAP.md) §Criterios NO negociables.
Razonamiento completo: [`AUDITORIA_ARQUITECTURA_2026-07.md`](../../../../docs/AUDITORIA_ARQUITECTURA_2026-07.md).

Las demás métricas de cada ficha (EV, profit factor, drawdown, condiciones de mercado)
siguen siendo válidas.

## Estado

| # | Edge | Estado |
|---|---|---|
| 01 | Overreaction | Implementado — `edges/overreaction.py`. **Sin validar**: no bate al mercado |
| 02 | Information Lag | Spec. Requiere feeds externos (GAP-04) |
| 03 | Market Structure | Spec |
| 04 | Narrative Decay | Spec. Requiere feeds externos |
| 05 | Event Volatility | Spec |
| 06 | Liquidity Vacuum | Spec. Requiere profundidad de libro (ya disponible vía WS) |
| 07 | Volume Anomaly | Spec |
| 08 | Spread Expansion | Spec |
| 09 | Momentum Exhaustion | Parcial — `edges/momentum.py` |
| 10 | Probability Drift | Spec |
| 11 | Consensus Divergence | Spec. Requiere feeds externos |
| 12 | Composite | Spec. Requiere ≥ 2 edges validados |
