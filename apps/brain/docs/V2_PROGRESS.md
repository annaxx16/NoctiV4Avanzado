# v2 — Progreso (Bloque A: Validación)

> El plan (RESTRUCTURE_PLAN §1) impone disciplina: **no se implementan los edges
> 2-11 hasta que OverreactionV1 esté validado** (Brier < 0.20 y EV+ en
> walk-forward). Por eso la v2 arranca por el Bloque A — la maquinaria de
> validación — y no por más edges.

## Qué se construyó (todo lógica pura, testeable sin infra)

| Módulo | Función |
|---|---|
| `backtest/metrics.py` | Brier, hit rate, EV/señal, profit factor, Sharpe, MaxDD + `passes_acceptance()` (§14) |
| `backtest/engine.py` | Replay deslizante anti-lookahead; reutiliza el slippage del paper; PnL contra outcome real |
| `backtest/walk_forward.py` | `calibrate` (grid σ×ema maximizando EV) + `walk_forward` (train→test OOS, degradación) |
| `backtest/loader.py` | Carga `book_snapshots`+`outcomes` desde Postgres → `SnapshotInput` |
| `validation/outcome_resolver.py` | Parse puro de resolución desde Gamma + job que persiste `outcomes` |
| `scheduler/outcomes_loop.py` | Loop background (cada 1h) que resuelve mercados vencidos |
| `scripts/run_backtest.py` | CLI: backtest + sensibilidad + walk-forward + veredicto go/no-go |

Extensión menor: `edges/overreaction.detect` acepta `sigma_threshold`/`ema_alpha`/
`min_snapshots` opcionales (defaults = settings) para el barrido de sensibilidad.

Cableado: `outcomes_loop` añadido a `BackgroundTasks`. No requiere migración
Alembic (la tabla `outcomes` ya existía).

## Tests (offline, sin DB)
`test_backtest_metrics.py`, `test_backtest_engine.py`, `test_walk_forward.py`,
`test_outcome_resolver.py` — 28 tests nuevos. Suite pura total: 42 verdes.

## Cómo se ejecuta

Requisitos: Postgres (Neon) + Redis (Upstash) configurados en `.env`
(ver [AUDIT_2026-06](AUDIT_2026-06.md) para el error de conexión y el setup).

```bash
.venv/bin/alembic upgrade head        # esquema (incluye outcomes)
.venv/bin/python scripts/run_api.py   # arranca API + loops (poller, exits, OUTCOMES, equity)
# el outcomes_loop empieza a resolver mercados vencidos cada 1h

# Una vez haya snapshots + outcomes acumulados:
.venv/bin/python scripts/run_backtest.py --since-days 30 --step 5
```

El backtest imprime métricas con parámetros de producción, la mejor combinación
del grid, el walk-forward por split y el veredicto §14 (APROBADO/NO APROBADO).

## Pendiente del Bloque A
- `FINDINGS_W1.md` con go/no-go (necesita ≥5000 snapshots + outcomes reales).
- Wiring de Brier/EV/Sharpe al dashboard Streamlit (endpoint `/portfolio` + panel).

## Siguiente (solo tras validar OverreactionV1)
Bloque B (CLOB API, feeds externos) y Bloque C (edges con datos internos: 3,6,7,
8,9,10). Hasta entonces, **disciplina**: no añadir edges.
