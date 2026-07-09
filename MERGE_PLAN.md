# Nocti — Plan de fusión Bot1 + UmbraNocti

**Decisión tomada:** cerebro Python (`brain`, ex-UmbraNocti) + brazo Node (`exec`, ex-Bot1),
comunicados por Redis. Objetivo de esta etapa: **consolidar y validar. Capital nuevo: $0.**
Bot1 sigue operando como hoy, sin tocar, hasta el final de la Fase 3.

---

## 1. Por qué esta forma

Bot1 tiene lo que a Umbra le falta (firma, CLOB, WebSocket, CTF on-chain) y le falta
lo que Umbra tiene (contabilidad persistente, risk engine, exit engine, backtest).
No se solapan casi en nada. Ninguno de los dos se reescribe.

Dos defectos de Bot1 que esta fusión cura directamente, y que hoy están abiertos en producción:

- El *permanent halt* al 40% de pérdida vive en un objeto en memoria (`bot-config.ts:263`).
  **No sobrevive a un restart del proceso.**
- Varias ejecuciones reales registran `recordTrade(0, 'smartMoney')` / `recordTrade(0, 'dipArb')`.
  Las 4 capas de riesgo se alimentan de PnL realizado, así que **en vivo están en gran parte ciegas**;
  solo el paper trading mueve el contador.

Un defecto de Umbra que esta fusión cura:

- El slippage simulado usa `volume_24hr` como proxy de liquidez (`execution/paper.py:47`),
  no el order book. Cualquier número de rentabilidad del backtest es, hoy, no comprobable.

---

## 2. Arquitectura

```
        Postgres  (única verdad contable)        Redis  (bus + kill-switch)
              ▲                                       ▲
              │                                       │
   ┌──────────┴────────────┐              ┌───────────┴───────────┐
   │  BRAIN   (Python 3.11)│              │  EXEC    (Node 18+/TS)│
   │  apps/brain           │──intents────▶│  apps/exec            │
   │                       │◀──fills──────│                       │
   │  edges · risk · exit  │◀──book───────│  CLOB · firma · CTF   │
   │  backtest · TA · API  │              │  WebSocket · swaps    │
   └───────────────────────┘              └───────────────────────┘
```

`brain` decide **qué** y **cuánto**. `exec` decide **cómo** llenar y firma.
`exec` nunca dimensiona una posición. `brain` nunca toca una clave privada.

### Monorepo

```
Nocti/
  apps/
    brain/          ← UmbraNoiti/ tal cual (src/umbra, tests, alembic, dashboard streamlit)
    exec/           ← Polymarket-bot/ tal cual (src/, dashboard react)
  packages/
    contracts/      ← esquemas del bus, versionados. Fuente única: JSON Schema
  infra/
    docker-compose.yml
  .env              ← único, gitignoreado
```

`poly-sdk/` en Bot1 está **vacía** y su nombre engaña: el SDK real es `src/`.
Se borra en la Fase 0.

---

## 3. Contrato del bus

Todo por Redis, que `brain` ya tiene levantado. `exec` necesita `ioredis` como dependencia nueva
(no está en `package.json:54-64`).

### 3.1 Market data — `book:{condition_id}`

**Este es el punto de integración más barato del proyecto.** `brain` ya lee esa clave
(`cache/book_cache.py:31`), con este JSON y TTL 60s:

```
{ condition_id, ts, best_bid, best_ask, last_trade_price, spread, liquidity_num, volume_24hr }
```

`exec` escribe exactamente esa forma desde el WebSocket oficial. **`brain` no cambia una línea
del lado lector.** Se sustituye el poller REST de 30s por un feed en tiempo real sin tocar
ni features, ni edges, ni risk.

Matiz: el WS da niveles de orderbook, no `liquidity_num`/`volume_24hr` (eso es de Gamma).
`exec` mergea ambos — `market-service.ts` ya hace ese merge. Y se **extiende** `CachedBook`
con dos campos opcionales, compatibles hacia atrás:

```
bids: [[price, size], ...]      # top N niveles
asks: [[price, size], ...]
```

Sin esto, la Fase 3 no puede medir nada. Con esto, el slippage deja de ser heurística.

### 3.2 Órdenes — `nocti:intents` → `nocti:fills`

Streams con consumer group (`XADD` / `XREADGROUP`), no pub/sub: pub/sub pierde mensajes
si el consumidor está caído, y aquí eso son órdenes.

**`nocti:intents`** — `brain` produce, `exec` consume (grupo `exec`):

```
intent_id        uuid v4, generado por brain
ts               ISO-8601
strategy         overreaction | momentum | arb | diparb | smartmoney
mode             shadow | live
condition_id     str
token_id         str
side             BUY | SELL
size_usd         decimal como string
limit_price      decimal como string
tif              GTC | FOK | IOC
max_slippage_bps int
expires_at       ISO-8601   ← exec descarta el intent si llega tarde
```

**`nocti:fills`** — `exec` produce, `brain` consume (grupo `brain`):

```
intent_id        el mismo
ts               ISO-8601
status           FILLED | PARTIAL | REJECTED | EXPIRED | ERROR
filled_shares    decimal como string
avg_price        decimal como string
notional_usd     decimal como string
fees_usd         decimal como string
order_id         str | ""
tx_hash          str | ""
error            str | ""
```

Todo decimal viaja **como string**. Nada de floats en el bus.

### 3.3 Las tres reglas no negociables

**Idempotencia.** Antes de firmar, `exec` hace `SET nocti:intent:{intent_id} 1 NX EX 86400`.
Si la clave existe, descarta y responde con el fill ya emitido. Sin esto, un restart de `brain`
reenvía intents no-ackeados y **duplicas órdenes con dinero real**. Es el bug más caro posible
de esta arquitectura.

**Un solo presupuesto de capital.** Hoy DipArb y overreaction dimensionarían cada uno contra el
bankroll completo, con la misma wallet. Todo sizing pasa por `risk/engine.py`. `exec` rechaza
cualquier intent cuyo `size_usd` no venga firmado por el risk engine.

**Halt simétrico.** `exec` lee `umbra:halt` antes de cada firma y **fail-closed**: si Redis no
responde, no firma. `brain` ya se comporta así (`risk/engine.py:66`). Si `exec` detecta un fallo
grave de ejecución, escribe `umbra:halt` + `umbra:halt:reason`.

---

## 4. Cambios de esquema

Menos de lo que parece. `signals` y `fills_paper` **ya tienen columna `mode`** (`db/models.py:130,168`)
y `config.py:19` ya declara `Literal["sim","paper","live"]` — el modo `live` existe como enum
sin code path. Se añade `shadow`.

Migraciones Alembic necesarias:

1. `fills_paper` → renombrar a `fills`. El nombre miente en cuanto haya fills reales.
2. `fills`: añadir `intent_id` (uuid, unique), `order_id`, `tx_hash`, `fees_usd`, `status`.
3. Nueva tabla `intents`: el registro de lo que `brain` pidió, independiente de lo que pasó.
   Sin ella no puedes auditar los rechazos de `exec`.
4. Nueva tabla `risk_state`: reemplaza el objeto `state` en memoria de Bot1.
   `peak_capital`, `daily_pnl`, `monthly_pnl`, `consecutive_losses`, `pause_until`, `halted_permanently`.
   **Esta tabla es la que hace que el halt permanente sobreviva a un restart.**

Nota: la columna ya es `Numeric(20,6)` en Postgres. La deuda de `float → Decimal(str(x))` está en
la capa de cálculo de Python, no en el almacenamiento. Se ataca en Fase 2, acotada al camino del dinero.

---

## 5. Fases

Cada fase termina con un criterio de aceptación verificable. No se pasa a la siguiente sin él.

### Fase 0 — Congelar y unificar (1-2 días)

- `git tag pre-merge` en ambos repos. Son la vuelta atrás.
- Crear monorepo `Nocti/`, mover ambos árboles sin cambiar una línea de lógica.
- Borrar `poly-sdk/` (vacía) y `repoinfo.md` (1.1 MB de ruido).
- Los dos `.env` con credenciales reales → un `.env` único, gitignoreado, fuera del árbol de git.
  **Verificar con `git log -p` que ninguna clave privada entró nunca en la historia de ninguno
  de los dos repos.** Si entró, esa wallet se rota antes de seguir.
- `docker-compose.yml` levanta: postgres, redis, brain, exec.

*Aceptación:* `docker compose up` arranca los cuatro servicios; `brain` pasa sus ~71 tests;
`exec` pasa sus tests unit. Cero cambios de comportamiento.

### Fase 1 — Market data en tiempo real (semana 1)

Riesgo cero: solo lectura, no se firma nada. Valida el bus antes de que haya dinero.

- `exec`: nuevo `src/bus/book-publisher.ts`. Suscribe el WS oficial a los mercados del universo,
  mergea con Gamma, escribe `book:{condition_id}` con la forma exacta que `brain` ya lee.
- `brain`: extender `CachedBook` con `bids`/`asks` opcionales. `poll_interval_sec` deja de ser
  la fuente; el poller queda como fallback si el book está stale (>60s TTL).
- `exec` lee el universo activo de `markets_active` (Postgres), no de su propia config.
  Una sola definición de qué mercados se vigilan.

*Aceptación:* durante 24h, `brain` recibe books con latencia < 2s (hoy: hasta 30s), cero gaps
> 60s, y el trigger de salida T0 `stale_book` no se dispara falsamente. El poller REST desactivado.

### Fase 2 — Contabilidad unificada (semana 1-2)

Aquí se cierra el agujero que hoy está abierto en producción.

- Migrar el `state` en memoria de Bot1 a la tabla `risk_state`. El halt permanente pasa a
  sobrevivir a restarts.
- Arreglar los `recordTrade(0, ...)`: el PnL realizado se calcula al cerrar, no al abrir.
  `exec` reporta el fill; `brain` calcula el PnL contra la posición en `portfolio_state`.
- Deduplicar el `CONFIG` divergente entre `bot-config.ts` y `bot-with-dashboard.ts`
  (`canTrade()`, `recordTrade()`, `state`, `setupBinanceAnalysis` están copy-pasteados con
  diferencias sutiles: `bot-config.ts` ignora los flags `*_ENABLED` del `.env`, y direct trading
  ejecuta en uno y solo loguea en el otro, `bot-config.ts:801`).
- Bugs concretos a matar de paso: `setInterval` registrado dos veces en `bot-with-dashboard.ts:868`
  (doble llamada a la API de Binance); el toggle de dry-run que reasigna `CONFIG.dryRun` dos veces
  (`:1082-1091`); `getWalletProfile().winRate` accedido con `as any` (`:416`), que filtra en silencio.
- `float → Decimal` en el camino del dinero de `brain` (`execution/paper.py`, `orchestrator.py`).
- Tests: el risk engine de `brain` no tiene test unitario por-compuerta. Se escriben los 11.
  Es lo más crítico del sistema y es lo menos cubierto.

*Aceptación:* matar el proceso de `exec` a mitad de sesión y comprobar que al reiniciar conserva
`peak_capital`, `daily_pnl` y el estado de halt. Un test que lo demuestre. Los 11 gates cubiertos.

### Fase 3 — Shadow execution (semana 2-3)

`brain` emite intents con `mode: shadow`. `exec` **no firma**: cotiza contra el book real y devuelve
el fill que *habría* obtenido. Se compara contra lo que `execution/paper.py` predijo.

- `exec`: consumer de `nocti:intents`, camino shadow completo (validación, dedup, cotización
  contra book real, publicación en `nocti:fills`). El camino `live` existe pero detrás de un
  flag que sigue apagado.
- `brain`: consumer de `nocti:fills`, escribe a `fills` con `mode='shadow'`.
- Reporte de divergencia: slippage predicho vs. slippage real, por estrategia y por tamaño.

*Aceptación:* 2 semanas de shadow con volumen suficiente. Sales sabiendo, con número, **cuánto
miente tu backtest**. Si el slippage real se come el edge de overreaction, lo sabes aquí, gratis.

---

## 6. Lo que NO se hace en esta etapa

Explícito, para que no se cuele:

- **No se pone capital nuevo.** Bot1 sigue con lo que ya tiene, operando como hoy.
- **No se activa el modo `live` del bus.** El code path se escribe en Fase 3 pero queda apagado.
- **Overreaction no toca dinero real.** El gate de `ROADMAP.md:306` (10 criterios no negociables)
  y `FINDINGS_W1.md` siguen en pie. Ese archivo **no existe todavía**: el edge principal de Umbra
  nunca ha sido validado, y su `P_fair` es hoy un passthrough de la EMA sin calibrar
  (`engine/probability.py`, GAP-01) — el Kelly está dimensionando sobre probabilidades que
  nadie ha verificado.
- **No se fusionan los dashboards.** Streamlit (research) y React (operación) conviven.
  No es el cuello de botella.
- **No se implementan los edges 2-11.** Siguen bloqueados por la propia disciplina de Umbra.

Cuando llegue el momento de ir en vivo (Fase 5, fuera de este plan), el orden es:
**arbitraje primero** — es estructural, gana porque YES+NO converge a $1, no predice nada, y ya
está probado en vivo. DipArb después. Overreaction último, y solo con veredicto go.
Cada estrategia cruza su propia puerta, con su propio presupuesto.

---

## 7. Riesgos de la fusión

| Riesgo | Mitigación |
|---|---|
| Órdenes duplicadas tras un restart | `SET NX` por `intent_id` antes de firmar (§3.3) |
| Doble sizing contra la misma wallet | Todo sizing por `risk/engine.py`; `exec` rechaza lo no firmado |
| Halt asimétrico (`brain` halta, `exec` sigue firmando) | `exec` lee `umbra:halt` fail-closed antes de cada firma |
| El contrato del bus deriva entre repos | `packages/contracts` versionado, JSON Schema, validado en CI |
| La fusión se usa de atajo para saltar el gate de validación | §6, y los presupuestos de capital separados por estrategia |
| Nonce colisiona entre estrategias | Una sola wallet, un solo firmante secuencial en `exec` |

El riesgo real no es técnico. Es que tener el motor de ejecución de Bot1 enchufado al cerebro
de Umbra hace **trivial** poner dinero detrás de un edge que nunca se validó, usando
"Bot1 ya opera en vivo" como excusa. Fusiona el código. No fusiones los permisos.
