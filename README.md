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

**No hay capital real en juego.** `DRY_RUN=true` y la clave privada de `exec` es un
placeholder. `brain` está en `MODE=sim`.

| Fase | Qué hace | Estado |
|---|---|---|
| 0 | Monorepo, contrato del bus, compose | Hecha |
| 1 | `exec` publica el book por WebSocket; `brain` gana profundidad de libro | Código listo, sin verificar contra infra |
| 2 | Contabilidad unificada; el halt sobrevive a los restarts | Pendiente |
| 3 | Shadow execution: cuánto miente el backtest | Pendiente |

Cada fase se enciende con un flag y se apaga con el mismo flag. Con
`NOCTI_BOOK_PUBLISHER_ENABLED=false`, `exec` se levanta pero no publica, y `brain` se
comporta exactamente como antes de la fusión: poller REST cada 30s contra Gamma.

Con el publicador encendido, `exec` escribe `book:{condition_id}` desde el WebSocket
oficial y `brain` toma de ahí el precio, pero **sigue haciendo su petición a Gamma**.
No es redundancia: es una sola llamada en batch por tick, y es la única fuente de
`active` / `accepting_orders`. El WebSocket aporta lo que Gamma no puede — precio de
hace ~1s en vez de 30s, y la profundidad real del libro, sin la cual el modelo de
slippage de `brain` es una heurística sobre `volume_24hr`.

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
cd apps/exec  && npm test                              # 67, unit (vitest)
cd apps/exec  && npm run test:integration              # llama a las APIs de verdad

cd apps/brain && .venv/Scripts/python -m pytest        # 89 de lógica pura, sin infra
```

Los 14 tests restantes de `brain` necesitan Postgres **y no pueden usar el de
producción**: escriben y borran filas. Levanta el del perfil `db` y apúntalos ahí:

```bash
docker compose --profile db up -d
export UMBRA_TEST_DATABASE_URL=postgresql+psycopg://umbra:umbra_dev@localhost:5434/umbra_test
cd apps/brain && .venv/Scripts/python -m alembic upgrade head && .venv/Scripts/python -m pytest
```

Sin esa variable, `tests/conftest.py` apunta a una base inexistente **a propósito**, y
rechaza cualquier URL cuyo nombre de base no contenga `test`. No es paranoia: una tanda
de `pytest` metió 31 snapshots sintéticos dentro de la serie histórica real, y hubo que
limpiarlos a mano. Fallar ruidosamente es la dirección segura del error.

Los tests anti-lookahead (`tests/leakage/`) no son opcionales. Si fallan, el backtest
está mintiendo y todo lo demás da igual.

## Mapa

| Ruta | Qué es |
|---|---|
| `apps/brain/src/umbra/edges/` | Las señales. Hoy: overreaction (principal) y momentum |
| `apps/brain/src/umbra/analytics/` | Capa de aprendizaje: pesos de edge, auditoría de señales |
| `apps/brain/src/umbra/research/` | Exploratorio: régimen, drawdown, series sintéticas |
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
