# Nocti

Bot de trading para Polymarket. Dos procesos, una wallet, una contabilidad.

```
        Postgres  (única verdad contable)        Redis  (bus + kill-switch)
              ▲                                       ▲
              │                                       │
   ┌──────────┴────────────┐              ┌───────────┴───────────┐
   │  BRAIN   (Python 3.11)│──intents────▶│  EXEC    (Node 20/TS) │
   │  apps/brain           │◀──fills──────│  apps/exec            │
   │                       │◀──book───────│                       │
   │  edges · risk · exit  │              │  CLOB · firma · CTF   │
   │  backtest · TA · API  │              │  WebSocket · swaps    │
   └───────────────────────┘              └───────────────────────┘
```

**`brain` decide qué y cuánto. `exec` decide cómo llenar, y es el único que toca la
clave privada.** `exec` nunca dimensiona una posición; `brain` nunca firma nada.

Nace de fusionar dos proyectos que resultaron ser las dos mitades del mismo bot:
`brain` era **umbraNocti** (todo cerebro, cero ejecución real) y `exec` era **Bot1 /
Polymarket-bot** (todo músculo, cero memoria). El plan completo y el porqué de cada
decisión está en [`MERGE_PLAN.md`](./MERGE_PLAN.md).

## Estado actual

**Fase 0 completada: el monorepo existe y nada cambió de comportamiento.**

El bus todavía no transporta nada. `NOCTI_BOOK_PUBLISHER_ENABLED=false` y
`NOCTI_EXEC_MODE=off`: `exec` se levanta pero no publica el book ni consume intents.
`brain` sigue con su poller REST de 30s. Es a propósito — cada fase se enciende con
un flag y se apaga con el mismo flag.

**No hay capital real en juego.** `DRY_RUN=true` y la clave privada de `exec` es un
placeholder. `brain` está en `MODE=sim`.

| Fase | Qué hace | Estado |
|---|---|---|
| 0 | Monorepo, contrato del bus, compose | Hecha |
| 1 | `exec` publica el book por WebSocket; `brain` deja el poller | Pendiente |
| 2 | Contabilidad unificada; el halt sobrevive a los restarts | Pendiente |
| 3 | Shadow execution: cuánto miente el backtest | Pendiente |

## Arrancar

Requisitos: Docker, o bien Python 3.11 + Node 20 para correr nativo.

```bash
cp .env.example .env      # y rellenar
docker compose up -d      # postgres, redis, brain, exec
docker compose --profile ui up -d   # + dashboards (Streamlit :8501, React :3001)
```

Todo queda bindeado a `127.0.0.1`. Para verlo en remoto, túnel SSH:
`ssh -L 8000:localhost:8000 usuario@host`.

Nativo, sin Docker:

```bash
# brain
cd apps/brain && python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt -r requirements-dev.txt && pip install -e .
python scripts/run_api.py

# exec
cd apps/exec && npm ci
npx tsx bot-with-dashboard.ts
```

## Tests

```bash
cd apps/brain && .venv/Scripts/python -m pytest        # ~71 tests, sin infra
cd apps/exec  && npm test                              # unit (vitest)
cd apps/exec  && npm run test:integration              # llama a las APIs de verdad
```

Los tests anti-lookahead de `brain` (`tests/leakage/`) no son opcionales. Si fallan,
el backtest está mintiendo y todo lo demás da igual.

## Mapa

| Ruta | Qué es |
|---|---|
| `apps/brain/src/umbra/edges/` | Las señales. Hoy: overreaction (principal) y momentum |
| `apps/brain/src/umbra/risk/engine.py` | Las 11 compuertas. Fail-closed. Lo más crítico del sistema |
| `apps/brain/src/umbra/engine/exit_engine.py` | Los 11 triggers de salida, priorizados |
| `apps/brain/src/umbra/backtest/` | Replay anti-lookahead, walk-forward, métricas |
| `apps/exec/src/services/trading-service.ts` | Órdenes contra el CLOB |
| `apps/exec/src/services/realtime-service-v2.ts` | WebSocket oficial, con re-suscripción |
| `apps/exec/src/clients/ctf-client.ts` | Split / merge / redeem on-chain |
| `packages/contracts/` | El contrato del bus. Fuente única de verdad |

## Antes de tocar dinero real

Está escrito en `apps/brain/ROADMAP.md:306` y sigue en pie: hay 10 criterios no
negociables, y hace falta un `FINDINGS_W1.md` con veredicto go que **todavía no
existe**. El edge de overreaction nunca ha sido validado, y su `P_fair` es hoy un
passthrough de la EMA sin calibrar — el Kelly está dimensionando sobre probabilidades
que nadie ha verificado.

Cuando llegue el momento, el orden es **arbitraje primero** (es estructural: gana
porque YES+NO converge a $1, no predice nada), DipArb después, y overreaction el
último. Cada estrategia cruza su propia puerta, con su propio presupuesto.

Fusionamos el código. No fusionamos los permisos.
