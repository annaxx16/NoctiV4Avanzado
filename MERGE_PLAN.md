# Nocti — Plan de fusión Bot1 + UmbraNocti

**Decisión tomada:** cerebro Python (`brain`, ex-UmbraNocti) + brazo Node (`exec`, ex-Bot1),
comunicados por Redis. Objetivo de esta etapa: **consolidar y validar. Capital nuevo: $0.**
Bot1 sigue operando como hoy, sin tocar, hasta el final de la Fase 3.

---

## 0. Cuál es el brain, y por qué

Había **dos** umbraNocti en el disco, y no eran la misma cosa:

- `Nocti/NoctiV2/UmbraNoiti` — el que **generó los datos**. Su capa `analytics/`
  (pesos de edge, rendimiento por edge, auditoría de señales) más su `learning_loop.py`
  llevaron la base de datos hasta la revisión de Alembic `7a8b9c0d1e2f`.
- `Nocti/NoctiV3/UmbraNoiti` — el mismo repo, un commit por delante (`d9053d8`), pero
  **sin la capa de aprendizaje**: la perdió porque en V2 nunca se commiteó. A cambio
  añadió el paquete `research/` (régimen, drawdown, sintéticos) y el despliegue Docker.

La base de datos manda. Estaba en `7a8b9c0d1e2f`, una revisión que **no existe en V3**:
`alembic upgrade head` habría fallado con *"Can't locate revision"*. Y las tablas que
solo conoce V2 tienen datos reales — `signal_audit` 1.029 filas, `trade_outcomes` 783,
`learning_snapshots` 30.

**`apps/brain` = el árbol de trabajo de V2, más el `research/` y el Docker de V3.**

Suerte que ayudó: `book_cache.py`, `poller.py` y `config.py` —los tres ficheros que toca
la Fase 1— son **idénticos** en V2 y V3. `scanner.py` solo difería en acentos. Reaplicar
la Fase 1 sobre V2 fue mecánico.

Queda una tensión sin resolver, y es del dueño del proyecto: `ROADMAP.md` y `STRATEGY.md`
prohíben meter ML hasta validar el edge, y la capa de pesos adaptativos de `analytics/`
es exactamente eso. Se conserva porque tiene datos y porque tirarla es irreversible, no
porque encaje con la disciplina escrita.

Copia intacta del árbol de V2, incluida la `analytics/` sin trackear:
`C:\Users\santi\_backup_umbra_v2_worktree`.

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

### Fase 1 — Market data en tiempo real (semana 1) — **implementada**

Riesgo cero: solo lectura, no se firma nada. Valida el bus antes de que haya dinero.

- `exec`: `src/bus/book.ts` (lógica pura) + `src/bus/book-publisher.ts` + entrada `nocti-exec.ts`.
  Suscribe el WS oficial a los mercados del universo y escribe `book:{condition_id}` con la
  forma exacta que `brain` ya lee, más `bids`/`asks` y `source`.
- `brain`: `CachedBook` extendido con `bids`/`asks`/`source` opcionales, compatible hacia atrás.
  El poller **prefiere** el book de WS si es fresco (`ws_book_max_age_sec`, 10s) y no lo pisa
  con datos de Gamma.
- `exec` **no habla con Postgres**. `brain` publica el universo en `nocti:universe` (con los
  `token_ids`, la liquidez y el volumen que Gamma da y el WS no). `exec` solo habla Redis y
  Polymarket, y no tiene credenciales de la base de datos.

**Corrección al plan original.** Decía *"el poller REST desactivado"* y *"`exec` lee el universo
de `markets_active`"*. Las dos cosas estaban mal:

1. El poller hace **una sola** petición en batch para todo el universo cada 30s, y es la única
   fuente de `active` / `accepting_orders` (columnas `NOT NULL` en `book_snapshots`). Apagarlo
   cambiaría 30s de latencia de precio por **5 minutos de ceguera** sobre si un mercado dejó de
   aceptar órdenes. El poller se queda como red de seguridad; el WS aporta lo que Gamma no puede:
   precio fresco y **profundidad real del libro**.
2. Darle Postgres a `exec` reparte las credenciales de la contabilidad entre dos procesos sin
   necesidad. El universo cabe en una clave de Redis.

Lo que la Fase 1 **no** trae: evaluación dirigida por eventos. `brain` sigue evaluando en cada
tick de 30s del poller, solo que ahora con precios de hace ~1s y con profundidad. Hacer que el
book dispare la evaluación es un paso aparte, y no hace falta para la Fase 3.

*Aceptación:* durante 24h con `NOCTI_BOOK_PUBLISHER_ENABLED=true`, `poller.tick` reporta
`from_ws` ≈ tamaño del universo, el trigger de salida T0 `stale_book` no se dispara falsamente,
y `skippedCrossed` se mantiene marginal frente a `published`.

### Fase 2 — Contabilidad unificada (semana 1-2)

**Contradicción resuelta: Redis.** Este plan decía "migrar el estado de riesgo de `exec` a
Postgres". Luego la Fase 1 estableció que **`exec` no habla con Postgres**. Se optó por la
salida (1) de las tres que había sobre la mesa:

1. **[hecho]** `exec` guarda su estado de riesgo en **Redis** (`nocti:exec:risk_state`), que con
   `appendonly` sobrevive a restarts. Mantiene la regla de los dos idiomas. Redis es más
   débil que Postgres para contabilidad, pero la propiedad que necesitamos —que el halt
   permanente sobreviva al proceso— la cumple.
2. `exec` lee y escribe su estado a través de la **API de brain**. Postgres sigue siendo la
   verdad, `exec` sigue sin credenciales de base de datos. Más correcto, más acoplado.
3. **`exec` pierde su risk engine entero.** brain es la única autoridad, como dice la regla
   del contrato ("un solo presupuesto de capital"). Sigue siendo el destino final, en la
   Fase 4. Cuando llegue, `src/risk/` de `exec` se borra entero.

Aquí se cierra el agujero del halt que no sobrevive a un restart.

- **[hecho]** El `state` en memoria de Bot1 vive ahora en `apps/exec/src/risk/`: `state.ts`
  (lógica pura), `store.ts` (Redis), `guard.ts` (write-through + fail-closed), `view.ts`
  (proyección para el dashboard). Los dos entrypoints comparten el módulo; sus copias
  divergidas de `canTrade()` y `recordTrade()` desaparecen.

  Se persisten los **hechos** (PnL, pico, rachas) y se derivan los **veredictos**: al cargar,
  `haltedPermanently` se recalcula contra `totalPnl`, así que una escritura rota no puede
  perder un halt. Fail-closed en tres sitios: si Redis no responde al arrancar no se opera;
  si una pérdida no se puede persistir la compuerta se cierra; si el estado está corrupto se
  repara hacia el lado conservador.

- **[hecho]** `recordTrade(0, ...)` era peor de lo que este plan suponía. No era solo "PnL no
  contabilizado": el `0` caía en la rama `else` de `if (profit < 0)`, de modo que **cada
  apertura se registraba como una victoria** — ponía `consecutiveLosses` a cero e incrementaba
  `consecutiveWins`. Como `consecutiveLosses` es lo que encoge la posición en una mala racha
  (`calculatePositionSize`), abrir una posición borraba la memoria de las pérdidas y el sizing
  dinámico crecía justo cuando debía encoger. En vivo, con dinero real.

  Ahora `recordOpen` (sin PnL, sin rachas) y `recordClose` (PnL realizado) son operaciones
  distintas. `dipArb` emitía las cuatro patas del ciclo (`leg1`, `leg2`, `exit`, `merge`) por
  el mismo camino: cuatro trades y cuatro victorias falsas por ciclo. Solo `leg1` abre.

  El PnL realizado de las patas de cierre **no se contabiliza todavía**: el evento no trae las
  shares llenadas y calcularlo en `exec` sería inventarlo. Entra por `nocti:fills` en la Fase 3,
  calculado por `brain` contra la posición. No contabilizar es más honesto que contabilizar mal.

- **[hecho]** Un doble conteo de paso: `simulateTrade(0, 'direct')` ya incrementaba
  `state.directTrades`, y la línea siguiente lo incrementaba otra vez.
- **[hecho]** Bugs quirúrgicos: `setInterval` registrado dos veces en `bot-with-dashboard.ts:868`
  (doble llamada a la API de Binance); el toggle de dry-run que reasignaba `CONFIG.dryRun` dos
  veces (`:1082-1091`); `getWalletProfile().winRate` accedido con `as any` (`:416`).
- Queda: deduplicar el resto del `CONFIG` divergente (`setupBinanceAnalysis`; `bot-config.ts`
  ignora los flags `*_ENABLED` del `.env`, y direct trading ejecuta en uno y solo loguea en el
  otro, `bot-config.ts:801`).
- **[hecho]** `float → Decimal` en el camino del dinero de `brain`. Se calculaba todo en
  float y se envolvía en `Decimal(str(x))` justo al guardar: aritmética binaria con un
  disfraz al final. Dos daños concretos, no estéticos:

  `cost_basis_released` se calculaba **dos veces** —una en `execute_close`, otra dentro
  de `_apply_close`— y solo la segunda respetaba el clamp de shares. `trade_outcomes`
  dividía el PnL realizado de una entre el cost basis de la otra: `return_pct` mentía.

  `proceeds` entraba a Postgres sin cuantizar y la base lo redondeaba a 6 decimales,
  mientras el PnL realizado se derivaba del valor sin redondear. Medido: la posición
  acumulaba `44.928853100793` donde el fill guardaba `44.928853`. La identidad
  `realized = notional - fees - cost_basis` no cerraba fila a fila, y sin ella no se
  puede reconstruir la contabilidad desde `fills_paper`.

  Ahora todo se cuantiza a la escala de su columna antes de que nada se derive de él, y
  el redondeo es adverso donde hay dirección segura: shares hacia abajo, fees hacia
  arriba, proceeds hacia abajo. El backtest no puede halagarse con el sexto decimal.
- **[hecho]** Los 11 gates de `brain`, en `tests/test_risk_engine.py` (33 tests).
  Los umbrales van clavados en un fixture: el suite no puede depender del `.env` del operador.

*Aceptación:* matar el proceso de `exec` a mitad de sesión y comprobar que al reiniciar conserva
`peak_capital`, `daily_pnl` y el estado de halt. Un test que lo demuestre. Los 11 gates cubiertos.

*Estado:* **cumplida.** `exec/src/risk/risk.test.ts` cubre las cuatro capas y el restart; el
criterio se comprobó además fuera de los tests, contra Redis real, matando el proceso con
`SIGKILL` y reiniciando también el propio Redis. Los 11 gates de `brain` están en
`tests/test_risk_engine.py`. 141 tests en `brain`, 91 en `exec`.

Solo queda abierto un punto menor de la lista: la deduplicación del `CONFIG` divergente entre
los dos entrypoints (`setupBinanceAnalysis` y los flags `*_ENABLED`). No bloquea la Fase 3:
`canTrade()`, `recordTrade()` y el estado de riesgo —lo que tocaba dinero— ya están unificados.

Se encontró y cerró, de paso, una trampa que no estaba en el plan: `alembic/env.py` ignoraba
`UMBRA_TEST_DATABASE_URL`, así que la receta de migración documentada en `docker-compose.yml`
apuntaba a la base de **producción**.

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
