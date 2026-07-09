# GG PMM Evolution

## Fase 2 - Signal Audit

Agregado incremental, sin cambiar decisiones de trading.

| Modulo | Funcion |
|---|---|
| `db.models.SignalAudit` | Tabla de auditoria para cada senal persistida por el orchestrator |
| `analytics/signal_audit.py` | Clasificacion de rechazos: risk, liquidity, exposure, composite, execution |
| `engine/orchestrator.py` | Escribe auditoria tras cada senal aceptada o rechazada |
| `GET /analytics/signal-funnel` | Funnel backend: generated, accepted, rejected, trades executed, trades closed, rates y reasons |

Migracion: `3c4d5e6f7a8b_add_signal_audit.py`.

Validacion enfocada:

```bash
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m pytest tests\test_signal_audit.py tests\test_orchestrator_e2e.py tests\test_orchestrator_paper.py
```

## Fase 3 - Outcome Engine

Agregado incremental sobre el cierre existente, sin cambiar triggers de salida.

| Modulo | Funcion |
|---|---|
| `db.models.TradeOutcome` | Resultado normalizado por cada CLOSE fill |
| `analytics/trade_outcomes.py` | Enlaza CLOSE con OPEN/senal original y calcula holding, return, profit/loss |
| `execution/paper.py` | Registra trade outcome despues de calcular realized PnL |
| `GET /analytics/trade-outcomes` | Lista outcomes recientes y permite filtrar por edge |

Migracion: `4d5e6f7a8b9c_add_trade_outcomes.py`.

Validacion enfocada:

```bash
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m pytest tests\test_exits_and_close.py::test_execute_close_produces_realized_pnl_and_closes_position tests\test_paper_execution.py
```

## Fase 4 - Edge Performance

Agregado incremental de metricas por edge, sin cambiar pesos ni ejecucion.

| Modulo | Funcion |
|---|---|
| `db.models.EdgePerformance` | Tabla agregada por `edge_name` |
| `analytics/edge_performance.py` | Calcula signals, trades, wins/losses, avg return, PF, Sharpe, expectancy, MaxDD y rollings |
| `analytics/signal_audit.py` | Refresca performance al auditar senales |
| `analytics/trade_outcomes.py` | Refresca performance al cerrar trades |
| `GET /analytics/edge-performance` | Lista performance por edge y permite refresh manual |

Migracion: `5e6f7a8b9c0d_add_edge_performance.py`.

Validacion enfocada:

```bash
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m pytest tests\test_signal_audit.py tests\test_edge_performance.py tests\test_exits_and_close.py::test_execute_close_produces_realized_pnl_and_closes_position tests\test_paper_execution.py
```

## Fase 5 - Dynamic Edge Weighting

Agregado incremental de pesos dinamicos. Los pesos quedan guardados y visibles,
pero no se aplican a ejecucion hasta que exista Composite Engine operativo.

| Modulo | Funcion |
|---|---|
| `db.models.EdgeWeight` | Tabla de pesos por edge |
| `analytics/edge_weights.py` | Calcula score y pesos con caps 5%-35% |
| `GET /analytics/edge-weights` | Lista pesos y permite refresh manual |

Migracion: `6f7a8b9c0d1e_add_edge_weights.py`.

Validacion enfocada:

```bash
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m pytest tests\test_edge_weights.py tests\test_edge_performance.py tests\test_signal_audit.py
```
