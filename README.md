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
| 1 | `exec` publica el book por WebSocket; `brain` gana profundidad de libro | Hecha y verificada (`from_ws=38/50`, books de 20 niveles) |
| 2 | Contabilidad unificada; el halt sobrevive a los restarts | Hecha |
| 3 | Shadow execution: cuánto miente el backtest | **Respondida.** Ver abajo |

**Miente por un factor 9,5.** La primera ventana de shadow (12→27 julio 2026, 345
intents cotizados contra el libro real) midió 191 bps de slippage donde el modelo
predecía 20 — y ese 20 no era una media, era el valor exacto de los 345: al modelo le
faltaba el término de spread, que es el coste dominante. El 52% de las órdenes eran de
menos de $5, pagaban 281 bps y no podían cubrir su propio peaje.

Corregido en `cf1005e`. La auditoría completa, con lo que sigue abierto, está en
[`docs/AUDITORIA_ARQUITECTURA_2026-07.md`](./docs/AUDITORIA_ARQUITECTURA_2026-07.md).

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
docker compose --profile app up -d --build
```

Un solo perfil levanta todo: `redis`, `brain`, `exec` y el dashboard de Streamlit.
**No existe un perfil `ui`** — lo hubo en el plan y nunca llegó al fichero; el perfil
`db` es solo el Postgres de tests en el 5434, y no hace falta para operar (el de
producción vive fuera de compose, en el 5432 de la máquina).

El `--build` no es opcional después de tocar código: las imágenes llevan el fuente
dentro, y sin reconstruir levantarías la versión anterior.

| Qué | Dónde |
|---|---|
| API de `brain` (FastAPI) | http://localhost:8000 — `/docs` para el índice |
| Dashboard de `brain` (Streamlit) | http://localhost:8501 |
| Dashboard de `exec` (React) | http://localhost:3001 |

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

## ⚠️ `scripts/reset_paper_state.py` borra más de lo que dice

Hace `TRUNCATE fills, portfolio_state, equity_snapshots, signals RESTART IDENTITY
**CASCADE**`, y ese `CASCADE` se lleva por delante `intents`, `signal_audit` y
`trade_outcomes` — es decir, **todas las mediciones de la Fase 3**. Su docstring
enumera lo que conserva, pero se escribió antes de que esas tres tablas existieran.

Para limpiar solo la contabilidad de papel conservando las mediciones:

```sql
TRUNCATE portfolio_state, equity_snapshots;   -- sin CASCADE, a propósito
```

Sin `CASCADE`, si algo dependiera de esas tablas Postgres falla en vez de borrarlo en
silencio. Haz backup antes igualmente:

```bash
pg_dump -h localhost -p 5432 -U umbra -d umbra \
  -t portfolio_state -t equity_snapshots -Fc -f backup.dump
```

## Antes de tocar dinero real

Los 10 criterios no negociables están en
[`apps/brain/ROADMAP.md`](./apps/brain/ROADMAP.md) §Criterios, y siguen en pie. Sigue
sin existir un `FINDINGS_W1.md` con veredicto go.

**El criterio 2 se midió por primera vez el 28/07/2026 y NO se cumple.** Sobre 1.799
eventos con outcome conocido, el Brier del modelo fue 0.0723 y el del precio de
mercado 0.0722; la diferencia emparejada da un IC del 95% de **[−0.00564, +0.00056]**,
que cruza el cero. No hay evidencia de que el modelo prediga mejor que el mercado.

Era lo esperable: `P_fair` es un passthrough de la EMA del mid —una versión suavizada
del precio— y no puede batir a aquello de lo que se deriva. El Kelly lleva desde
siempre dimensionando sobre eso. Cerrar GAP-01 es la prioridad, y ahora es posible:
hay 98 outcomes resueltos y una métrica que dirá si la calibración funciona.

> El criterio 2 decía «Brier < 0.20» hasta ese día. Se cambió a **batir al mercado con
> significancia** porque el umbral absoluto lo cumplían tanto el modelo como el
> mercado: no premiaba acertar, premiaba operar donde es fácil acertar. El detalle
> está en [`docs/AUDITORIA_ARQUITECTURA_2026-07.md`](./docs/AUDITORIA_ARQUITECTURA_2026-07.md).

Cuando llegue el momento, el orden es **arbitraje primero** (es estructural: gana
porque YES+NO converge a $1, no predice nada), DipArb después, y overreaction el
último. Cada estrategia cruza su propia puerta, con su propio presupuesto.

Fusionamos el código. No fusionamos los permisos.
