# Auditoría de arquitectura — monorepo Nocti, 27-28 julio 2026

> **Estado a 28/07/2026, 23:00.** Este documento empezó como auditoría estática y
> acabó siendo el registro de dos días en los que el sistema se midió a sí mismo por
> primera vez. Lo esencial, para quien lo lea con prisa:
>
> | | |
> |---|---|
> | Modelo de slippage | **Corregido y validado.** Divergencia mediana +54 → −0,0 bps sobre 298 medidas fuera de muestra |
> | Freno de drawdown | **Estaba ciego.** Perdió $914 de papel en una noche. Corregido |
> | Universo | **42% eran mercados irresolubles.** Filtrado a Yes/No |
> | Resolución de outcomes | **Llevaba semanas devolviendo cero.** Corregido: 0 → 98 filas |
> | Brier del modelo | **0.0723 vs 0.0722 del mercado.** Sin evidencia de edge |
> | Gate §14 | **Reescrito**: batir al mercado con significancia, no un umbral absoluto |
> | Dinero real en juego | **Ninguno, en ningún momento.** Cero fills en modo `live` |
>
> Commits: `cf1005e` · `ff37dc2` · `25b5ffd` · `3bb5c55` · `ef38a46`

Auditoría del monorepo completo (`apps/brain`, `apps/exec`, `packages/contracts`)
con vistas a convertirlo en un framework cuantitativo multi-exchange. No se ha
escrito código: este documento es el entregable previo.

Alcance: 15.572 líneas Python en `brain`, 45.211 líneas TypeScript en `exec`,
4 ficheros de contrato. Se han leído los módulos del camino crítico completos y
se ha consultado la base de datos de producción en modo lectura.

---

## 0. Veredicto

**La arquitectura no es el problema de este proyecto.**

Esperaba encontrar un bot acumulado por capas y me he encontrado con un sistema
con separación de responsabilidades real, frontera Decimal/float explícita,
outbox transaccional correcto, kill-switch fail-closed simétrico, tests
anti-lookahead, y comentarios que explican *por qué* en vez de *qué*. El
`packages/contracts/README.md` y la docstring de `Fill` son mejores que la media
de lo que se ve en mesas profesionales.

Aplicar ahora Hexagonal + DDD + Plugin Pattern sobre esto sería exactamente la
sobreingeniería que tu propia filosofía prohíbe. La capa de puertos que hace
falta para el segundo exchange son **dos interfaces**, no una reescritura, y las
describo en §7.

El problema real es económico y lo acabo de medir. Va primero porque invalida
condicionalmente todo lo demás.

---

## 1. El hallazgo que bloquea el resto

La Fase 3 se construyó para responder una pregunta: *¿cuánto miente el
backtest?* La ventana de shadow lleva desde el 12 de julio acumulando y **nadie
había leído el resultado**. Lo he ejecutado.

```
intents emitidos: 345   medibles: 315
estados: FILLED=251  REJECTED=94

grupo             n   esperado    real    divergencia    p50      p90
TODO            315       20.0   191.0        +171.0   +54.0   +381.8
```

El modelo predice 20 bps. El libro real cobra 191. **La divergencia es de +171
bps, un factor 9,5×.**

Y hay algo peor que la magnitud. `expected` vale exactamente `20.0` en los 345
intents, sin una sola excepción. El modelo de `execution/paper.py:99` es

```python
ratio = notional_usd / liquidity_usd
return min(base + size_factor * ratio, cap)
```

Con nocionales de ~$11 contra liquidez de ~$5.000, el `ratio` vale 0,002 y el
término de tamaño aporta **0,4 bps**. En producción el modelo no es una función:
es la constante `slippage_base_bps`. Nunca ha variado y nunca variará en este
régimen de tamaños.

La causa de fondo es que **el modelo no tiene término de spread**, y el spread es
el coste dominante. El spread medio del universo es 0,0048 → medio spread sobre
un mid de $0,50 son ~48 bps. La divergencia mediana medida es +54 bps. El caso
típico se explica entero por el término que falta.

### 1.1 La mitad del flujo es económicamente inviable por construcción

Desglosando por tamaño, el resultado es monótono y limpio:

```
bucket        n   rechazados  slippage medio   p50
<$5         181           64         281 bps   139
$5-10        51            0         102 bps    62
$10-25       54            0          57 bps    55
>=$25        29            0          36 bps    24
```

**El 52% de los intents son de menos de $5**, pagan 281 bps de media, y
concentran **el 100% de los rechazos de `exec`** (64 de 64 en esta tabla; los
94 totales incluyen filas sin slippage medible). El p50 converge hacia ~24-55 bps
—el medio spread— según sube el tamaño: la cola es impacto de libro fino, y el
libro fino solo lo tocan las órdenes que no deberían existir.

La aritmética que importa: un edge en el mínimo (`min_edge = 0.02`) sobre un
contrato a $0,50 son 400 bps de retorno bruto esperado. El coste de ida y vuelta
en el tramo `<$5` es 281 × 2 = **562 bps**. Esas órdenes tienen EV negativo
*antes* de que el edge se ponga a prueba. No es que el edge sea malo; es que no
puede pagar su propio peaje.

### 1.2 De dónde salen las órdenes de $2

De dos sitios, y ninguno es un bug:

`risk/sizer.py` dimensiona Kelly fraccional: `notional = κ · bankroll · f*`, con
κ=0,15 y bankroll=$1.000, o sea `150 · f*`. Un edge justo en el umbral da
`f* ≈ 0,04` → $6. Los edges marginales producen órdenes marginales, y el umbral
`min_edge` está fijado en espacio de probabilidad mientras el coste vive en
espacio de spread. Nadie conectó las dos unidades.

Después, `risk/engine.py` tiene once compuertas y las cuatro últimas (max risk,
exposición por mercado, exposición bruta, reserva de caja) **recortan el nocional
multiplicativamente sin suelo**:

```python
ratio = market_room / notional
notional = market_room
shares = shares * ratio
```

Un $6 que atraviesa tres recortes sale convertido en $2. El engine sabe decir
"no" y sabe decir "menos", pero no sabe decir *"tan poco que no merece la pena"*.
Falta una compuerta de suelo. Es la corrección de mayor relación
beneficio/esfuerzo de todo el repositorio.

### 1.3 Qué significa esto para el gate §14

`ROADMAP.md:306` lista diez criterios no negociables antes de tocar dinero. La
medición de la Fase 3 no invalida el edge —el edge sigue sin validarse, que es
distinto— pero sí invalida **el backtest que iba a validarlo**: `AUDIT_2026-07.md`
ya advertía que el backtest era optimista respecto a la ejecución real, y ahora
sabemos por cuánto. Cualquier `FINDINGS_W1.md` emitido con el modelo de 20 bps
sería un veredicto sobre una estrategia que no existe.

Corolario incómodo pero útil: **momentum, no overreaction, es quien opera.**
247 de 315 intents (78%) son de `momentum`; overreaction aporta 68. Toda la
documentación, el `STRATEGY.md` y el gate de validación giran alrededor de
overreaction, y el orchestrator lo prueba primero — pero es el edge secundario,
activado por un flag con default `True`, quien genera cuatro de cada cinco
órdenes. La cosa que estás midiendo no es la cosa que estás documentando.

---

### 1.4 La salida no se ha medido nunca, y es donde vive el coste

`stage_intent` se llama desde un solo sitio (`orchestrator.py:220`), que es el
camino de apertura. **Los cierres no pasan por el bus.** La Fase 3 midió 345
entradas; los cierres del mismo periodo son 4.268, y todos se valoraron con la
constante de 20 bps que acabamos de demostrar falsa.

```
FILLS           n       bps        nocional
OPEN  (paper)   480      20 (modelo)   $7.173,95
CLOSE (sim)   4.268      20 (modelo)   $9.093,55   ← nunca contrastado
SHADOW          345     191 (REAL)                 ← solo entradas
```

Y al desglosar los cierres aparece el problema de fondo:

```
cierres  <$1    n=3.173   nocional total $  438,49
         $1-5   n=  671                  $1.561,14
         $5-10  n=  195                  $1.360,83
         >=$10  n=  229                  $5.733,09
```

**El 74% de los cierres mueve el 4,8% del nocional.** 275 posiciones produjeron
4.268 cierres: 15,5 por posición, de $2,13 de media. Todos los `fraction` del
código valen 1.0, así que no son salidas parciales: son ciclos de
abrir → cerrar → reabrir sobre posiciones diminutas, y **cada vuelta paga el
spread entero**. La causa raíz es la misma de §1.2 —posiciones que nacen
demasiado pequeñas— así que el suelo de nocional en la entrada la ataja en
origen. Pero el camino de salida no tiene suelo propio y nadie lo ha medido.

Corrigiendo el PnL declarado por el coste real de cruzar:

```
PnL declarado (4.268 cierres)                       -$148,39
  si el cruce real cuesta  63 bps (p50 medido)      -$218,34
  si cuesta 116 bps (media de los FILLED)           -$304,56
  si cuesta 191 bps (media de todo)                 -$426,56
```

El sistema no está perdiendo $148. Está perdiendo entre dos y tres veces eso, y
la diferencia es exactamente el peaje que el modelo no cobraba.

---

## 1bis. Qué se ha corregido (28 julio 2026)

**Modelo de slippage** (`execution/paper.py`). Se añade el término de medio
spread, medido contra el precio del token que se compra —no contra el mid del
YES: los libros de YES y NO son espejo en absoluto pero no en bps—. El
coeficiente no se eligió: se ajustó sobre los 315 fills por error absoluto
mediano, y el óptimo resultó `k=1.0, size_factor≈0`. `slippage_base_bps` pasa de
sumando a **suelo**; el tope sube de 500 a 1500 porque con término de spread el
anterior truncaba justo donde el modelo empezaba a acertar. El término de impacto
se conserva y se documenta como **no validado**: a los tamaños actuales
(ratios 1e-5 a 5e-4) no hay variación con la que ajustarlo.

Validación sobre los 240 fills emparejables con su libro:

```
                 err p50    |err| p50   subestima
modelo viejo      -43,0        43,0        77%
modelo nuevo       +0,1         0,4        43%
```

> ⚠️ Ese ajuste es **en parte tautológico**: predicción y medición salen ahora del
> mismo libro. La prueba real es la ventana siguiente, donde la predicción se
> emite antes de que `exec` cotice.

**Compuerta 12** (`risk/engine.py`). Suelo de nocional, evaluado **después** de
los recortes 8-11 — antes de ellos dejaría pasar justo las órdenes que existe
para matar, porque en ese punto todavía no son pequeñas. Rechaza, no recorta:
media orden pequeña sigue siendo pequeña. A 0 el comportamiento es el anterior.

**Propagación del spread.** `orchestrator` toma liquidez y spread del **mismo**
snapshot y los pasa a `paper_execute` y a `stage_intent`, para que la resta de la
Fase 3 siga comparando predicciones hechas sobre un solo estado del libro.
`exit_engine._book_context_for` hace lo propio en la venta.

Tests: +12 (4 de la compuerta 12, 8 del modelo de spread). Suite igual que el
baseline —16 failed / 21 errors, todos de Postgres ausente (`AUDIT_2026-07.md:207`)—
y 253 → 265 pasando. Ruff limpio sobre lo tocado.

**Lo que NO se ha corregido y sigue abierto:** el suelo en el camino de salida
(§1.4), el gate de spread (§1bis.1), `MIN_EDGE` (§1bis.2) y la calibración de
`p_fair` (GAP-01).

### 1bis.1 El gate de spread es el instrumento equivocado

`max_spread_for_entry = 0.04` es un umbral **absoluto** sobre un coste que es
**relativo**: lo que duele no es el spread en céntimos, sino el spread dividido
por el precio del contrato. Apretarlo apenas sirve:

```
gate      intents que pasan   slippage real p50
0.040 (hoy)   240/240 (100%)        63 bps
0.020         221/240 ( 92%)        61 bps
0.010         187/240 ( 78%)        57 bps
0.005          92/240 ( 38%)        20 bps
```

De 0,04 a 0,01 se pierde el 22% del flujo para ganar 6 bps. El gate correcto es
relativo, y ahora que `half_spread_bps()` existe cuesta tres líneas: rechazar si
el medio spread supera N bps. Se deja para la ventana siguiente por no mezclar
variables.

### 1bis.2 `MIN_EDGE` vivo admite señales que no cubren un solo cruce

El `.env` del operador afloja seis umbrales respecto al código:

| parámetro | default | `.env` |
|---|---|---|
| `MIN_EDGE` | 0.02 | **0.003** |
| `KELLY_KAPPA` | 0.15 | 0.25 |
| `MIN_SIGNAL_CONFIDENCE` | 0.30 | 0.10 |
| `OVERREACTION_SIGMA_THRESHOLD` | 3.0 | 2.0 |
| `MAX_TIME_TO_RESOLUTION_HOURS_FLOOR` | 2.0 | 0.5 |
| `UNIVERSE_TOP_N` | 20 | 50 |

Con `MIN_EDGE = 0.003` sobre un contrato a $0,50 el edge mínimo son **60 bps
brutos**. El cruce mediano medido son 63 bps; la ida y vuelta, 126. **El umbral
acepta señales cuyo edge completo es menor que un solo cruce.** El punto de
equilibrio está en `MIN_EDGE ≈ 0.0063` a ese precio, y con margen razonable en
~0.013 — cerca del 0.02 que traía el código.

Esto explica el PnL de §1.4 sin necesidad de invocar al edge: el sistema está
configurado para aceptar operaciones cuyo coste conocido excede su beneficio
esperado. **La ventana de medición se corre igualmente con 0.003 a propósito**:
lo que se mide es la convergencia del modelo de slippage, y un umbral bajo da más
muestra. El PnL de esa ventana será negativo por construcción y **no debe leerse
como un veredicto sobre el edge**.

### 1bis.3 `env_file` se resuelve contra el CWD

`config.py` declara `env_file=".env"`, que `pydantic-settings` resuelve relativo
al directorio de trabajo. Bajo compose no importa —`env_file:` inyecta las
variables como entorno real— pero un arranque nativo desde `apps/brain` **no
encuentra el `.env` de la raíz y corre con los defaults del código**, que son
otros seis valores distintos. Falla en silencio y en la dirección de parecer que
funciona. `scripts/run_api.py` se lanza así en el README.

### 1bis.4 Configuración de la ventana 28-30 julio

`.env` (respaldado en `.env.bak_20260728`): `BANKROLL_USD=5000` y
`MIN_NOTIONAL_USD=10`. El bankroll sube solo para esta ventana — con 1.000 el
suelo dejaba pasar 5,7 intents/día y dos días no dan muestra:

| bankroll | sobreviven al suelo | intents/día |
|---|---|---|
| $1.000 | 25% | 5,7 |
| $3.000 | 50% | 11,5 |
| **$5.000** | **68%** | **15,6** |
| $10.000 | 93% | 21,4 |

> ⚠️ **Devolver a 1.000 antes de cualquier paso a live.** El bankroll es también
> el denominador de `max_gross_exposure_pct` y de los gates de drawdown.

---

## 1ter. La noche del 28 de julio

Lo que la ventana 2 encontró, en orden de aparición.

### El freno de drawdown estaba ciego

La equity de papel cayó **15,82%** (de $4.853,46 a $4.085,63) con `dd_halt_pct` en
0,15. El halt no saltó. El peor drawdown que la compuerta llegó a *ver* fue
**−14,62%**: se libró por 38 centésimas.

`portfolio_snapshot` medía el pico «desde la última vez que la cartera estuvo plana»,
y sin posiciones abiertas hacía `peak = equity` — drawdown 0,00. La intención era
legítima: que un pico antiguo no dejara el bot en halt para siempre. El fallo es que
**el criterio lo marcaba la estrategia, no el reloj**. Esta estrategia se queda plana
constantemente —102 veces en 24 horas— y cada una borraba la memoria del pico.

Corregido en `25b5ffd`: el pico se busca en una ventana de reloj
(`dd_peak_window_hours`, 48h) y se mira siempre, haya posiciones o no.

> **La lección general, que vale más que el bug**: cualquier mecanismo de riesgo cuyo
> periodo lo defina la propia actividad del bot puede ser anulado por esa actividad. Al
> revisar un gate, preguntar siempre quién decide su ventana. Si la respuesta es «la
> estrategia», es un bug esperando.

### El 42% del universo eran mercados irresolubles

21 de 50 mercados activos tenían outcomes con nombre propio —`["Cleveland Guardians",
"Cincinnati Reds"]`, `["Imperial", "BESTIA"]`, `["Over", "Under"]`— y el **31% de las
señales aceptadas** (342 de 1.098) vivía ahí.

`resolve_yes_outcome` solo lee mercados con etiqueta «Yes», así que esos nunca llegan a
`outcomes`. Sin fila en `outcomes` el trigger T1 del exit engine no salta jamás: **esas
posiciones no pueden cerrarse por haber acertado, solo por stop-loss, TTL o fricción.**
Y `stage_intent` los descartaba con `token_no_resoluble`, así que tampoco se medían.

Además la premisa del edge no traslada. En un mercado Yes/No sobre un evento, un salto
de precio puede ser pánico; en «Guardians vs Reds» suele ser información —cambia el
pitcher, hay una lesión— y no hay media a la que revertir.

Corregido en `3bb5c55`. El universo quedó **100% Yes/No sin perder tamaño**: el scanner
simplemente paginó más hondo.

### El resolver de outcomes llevaba semanas devolviendo cero

`outcomes` tenía **cero filas** con 187 mercados Yes/No vencidos esperando. No estaba
hambriento: estaba roto. Medido contra la API real:

```
sin el parámetro  ->  1 de 20 devueltos,  0 cerrados
closed=true       -> 19 de 20 devueltos, 19 cerrados
```

**`/markets` de Gamma filtra a mercados no cerrados por defecto**, aunque preguntes por
`condition_id` exacto. El resolver pedía los vencidos sin decir que los quería cerrados.
Fallaba en silencio: sin excepción, sin log de error, solo un diccionario casi vacío.

Segundo fallo del mismo barrido: `_markets_pending_resolution` no filtraba por Yes/No,
así que los 144 irresolubles ocupaban el lote hasta el `limit` una y otra vez y nunca
salían de `notin_(resolved)`. **Un barrido con `limit` y sin filtrar por "lo que
realmente puedo procesar" se atasca en lo que no puede procesar.**

Efecto inmediato: **0 → 98 filas**, 37 mercados calibrables, 136 señales puntuables.

### Y con eso, el primer Brier del proyecto

| muestra | n | Brier modelo | Brier mercado |
|---|---|---|---|
| todas las señales | 1.799 | 0.0723 | 0.0722 |
| solo aceptadas | 136 | 0.1104 | 0.1126 |

```
diferencia media (modelo - mercado):  -0.00222
IC 95% bootstrap emparejado:          [-0.00564, +0.00056]   ← cruza el cero
```

**No hay evidencia de que el modelo prediga mejor que el propio precio de mercado.**

Era lo esperable —`compute_p_fair` es un passthrough de la EMA del mid, o sea una
versión suavizada del precio, y no puede batir a aquello de lo que se deriva— pero
ahora es un número medido y no un argumento.

**Y no leer el 0,07 como aprobado.** Está muy por debajo del 0,20 que pedía el criterio
§14, pero ese umbral es engañoso: si la mayoría de mercados resuelven cerca de su
precio, copiar el precio ya saca un Brier bajo. El umbral absoluto no premiaba acertar,
premiaba operar donde es fácil acertar. Por eso el gate se reescribió (`ef38a46`) para
exigir **batir al mercado con significancia**, con `beats_baseline` cierto solo si el
IC del 95% queda entero bajo cero, semilla fija para que el veredicto sea reproducible,
y fail-closed si no hay referencia.

### Lo que esto cambia en el plan

GAP-01 —calibrar `p_fair`— pasa de *inatacable* a *la prioridad*. Ayer no se podía
calibrar porque no había un solo outcome; hoy hay 98 y una métrica que dirá si la
calibración funciona. Mientras `p_fair` sea la EMA, Kelly dimensiona sobre el precio de
mercado disfrazado de probabilidad propia.

---

## 2. Mapa del proyecto

```
NoctiV3/
├── apps/brain/          Python 3.11 · 15,5k LOC · 30 ficheros de test
│   └── src/umbra/
│       ├── polymarket/     ← ÚNICO acoplamiento real al exchange (client+schemas)
│       ├── universe/       scanner: qué mercados vigilar
│       ├── scheduler/      7 loops: poller, exits, equity, ohlc, outcomes, learning
│       ├── features/       calculator + loader (as_of estricto)
│       ├── edges/          overreaction, momentum, common
│       ├── engine/         orchestrator · probability · exit_engine (11 triggers)
│       ├── risk/           engine (11 compuertas) · sizer (Kelly fraccional)
│       ├── execution/      paper.py — simulación de fills, Decimal end-to-end
│       ├── portfolio/      manager: equity, DD, mark-to-market
│       ├── backtest/       engine · loader · metrics · walk_forward
│       ├── analytics/      edge_performance · edge_weights · learning · shadow_divergence
│       ├── bus/            contract · intents (outbox) · fills · tokens
│       ├── db/             models (14 tablas) · session · base
│       ├── cache/          book_cache · universe_cache · redis_client
│       ├── ta/             ohlc · levels · trend · signal
│       ├── research/       regime · drawdown · series · synthetic
│       └── api/            app.py — FastAPI, 742 LOC
│
├── apps/exec/           Node 20 / TS · 45,2k LOC
│   └── src/
│       ├── bus/            intent-consumer · book-publisher · quote  ← lo vivo
│       ├── services/       trading · realtime-v2 · market            ← lo vivo
│       ├── services/       dip-arb · smart-money · arbitrage · swap · binance ← LEGADO
│       ├── clients/        ctf · gamma · data-api · bridge · subgraph
│       ├── risk/           guard · state · store · view
│       └── core/           types · cache · errors · rate-limiter
│
├── packages/contracts/  4 ficheros — fuente única de verdad del bus
└── UmbraNoiti/          20.785 ficheros de copia pre-fusión, sin versionar
```

---

## 3. Flujo de datos y de ejecución

```
                    ┌──────────── Gamma REST (batch, 30s) ────────────┐
                    │  única fuente de active / accepting_orders      │
                    ▼                                                 │
  scanner ──> markets_active ──> poller.poll_once() ──┐               │
                                                       │               │
  exec ──WS CLOB──> book:{cid} (Redis, TTL 60s) ───────┤ precios si    │
       (bids/asks: profundidad real)                   │ edad < 10s    │
                                                       ▼               │
                                        build_snapshot() ─────────────┘
                                                       │
                                                       ▼
                                            book_snapshots (Postgres)
                                                       │
                              ┌────────────────────────┘
                              ▼
                    evaluate_market(cid)
                              │
              ┌───────────────┼──────────────────────────────┐
              ▼               ▼                              ▼
      detect_overreaction  detect_momentum            (fallback si None)
              └───────┬───────┘
                      ▼
              ta_evaluate_entry ──reject──> Signal(accepted=False) + audit ──▶ fin
                      │
                      ▼
              compute_p_fair()  ⚠ PASSTHROUGH sin calibrar
                      ▼
              size_position()   Kelly fraccional κ=0.15
                      ▼
              risk.check()      11 compuertas, corta en la primera
                      │
         ┌────────────┴────────────┐
    rechazada                  aceptada
         │                         │
         ▼                         ▼
    Signal + audit          Signal + audit
                                   │
                     ┌─────────────┴─────────────┐
                     ▼                           ▼
            paper.execute_signal()      stage_intent()  [solo mode=shadow]
                     │                           │ fila en `intents`, misma tx
                     ▼                           │
            fills + portfolio_state              │  ── COMMIT ──
                     │                           ▼
                     │                    publish_pending() → XADD nocti:intents
                     │                           │
                     │                           ▼
                     │              exec: SET nocti:intent:{id} NX  (idempotencia)
                     │                    camina el libro real, NO firma
                     │                           ▼
                     │                    XADD nocti:fills
                     │                           ▼
                     └──────────────▶  bus/fills.py → Fill(mode='shadow',
                                       action='SHADOW')  ← instrumento de medida,
                                                            NO contabilidad
                                                 ▼
                                        shadow_divergence.py → la resta
```

Los siete loops de `scheduler/` corren en paralelo bajo `supervisor.py`: poller
(30s), exits (60s), equity (60s), ohlc (60s), outcomes (1h), learning.

**Lo que está bien y merece decirse:** el outbox está resuelto correctamente. La
fila de `intents` se escribe dentro de la transacción de la señal; la publicación
a Redis ocurre *después* del commit y en sesión propia; la reentrega se
neutraliza con `SET NX` en exec y con `unique` sobre `intent_id` en Postgres.
Entrega al menos una vez + consumo idempotente, que es la única combinación
correcta cuando dos almacenes no comparten transacción. Mucha gente con más
presupuesto que tú publica antes de commitear.

---

## 4. Dependencias

`brain` (fijadas, todas): fastapi 0.115.4, uvicorn, pydantic 2.9.2,
pydantic-settings, structlog, httpx, psycopg[binary] 3.2.3, redis 5.2.0,
sqlalchemy 2.0.35, alembic, tenacity, greenlet 3.5.1, streamlit, pandas, plotly.
Dev: ruff 0.7.4, mypy 1.13.0, pytest, pytest-asyncio.

`exec`: @polymarket/clob-client ^5.1.3, @polymarket/real-time-data-client,
ethers **5** (mayor obsoleta), ioredis, ws, bottleneck, @catalyst-team/cache.
Dev: tsx, typescript 5.7, vitest 2.1.8.

Observaciones: no hay Polars, PyArrow ni DuckDB pese a figurar en tus
preferencias — y **hoy no hacen falta**: el volumen es de decenas de miles de
filas y Postgres las sirve sin despeinarse. Introducirlos ahora sería complejidad
sin problema que resolver. `ethers 5` sí merece un plan de migración a v6 antes
de tocar dinero, porque es la librería que firma.

---

## 5. Deuda técnica

| # | Sev | Área | Hallazgo | Estado |
|---|-----|------|----------|--------|
| 1 | 🔴 | `execution/paper.py:99` | Modelo de slippage sin término de spread; en producción una constante de 20 bps. Divergencia +171 bps | ✅ `cf1005e` |
| 2 | 🔴 | `risk/engine.py:285-339` | Cuatro compuertas recortan nocional sin suelo → órdenes de $2 con EV negativo. 52% del flujo | ✅ `cf1005e` |
| 1b | 🔴 | `portfolio/manager.py:229` | El pico de drawdown se reseteaba en cada momento plano → freno ciego. −15,82% real, −14,62% visto | ✅ `25b5ffd` |
| 1c | 🔴 | `universe/scanner.py` | 42% del universo eran mercados con outcome de nombre propio, irresolubles | ✅ `3bb5c55` |
| 1d | 🔴 | `polymarket/client.py` | Gamma filtra a no-cerrados por defecto → `outcomes` vacía durante semanas | ✅ `ef38a46`* |
| 1e | 🟠 | `backtest/metrics.py` | El gate §14 usaba un umbral absoluto que un sistema sin edge cumple | ✅ `ef38a46` |
| 3 | 🔴 | `engine/probability.py:22` | `compute_p_fair` es passthrough de la EMA. Kelly dimensiona sobre probabilidades no calibradas (GAP-01) | **ABIERTO — prioridad 1** |
| 3b | 🔴 | `bus/intents.py` | El coste de SALIDA nunca se ha medido: `stage_intent` solo en la apertura | **ABIERTO** |
| 3c | 🟠 | `engine/exit_engine.py` | Sin suelo de nocional en la salida. 3.173 cierres de <$1 moviendo el 4,8% del nocional | **ABIERTO** |
| 3d | 🟠 | `scripts/reset_paper_state.py` | `TRUNCATE ... CASCADE` destruye `intents`, `signal_audit` y `trade_outcomes`. Su docstring no lo dice | **ABIERTO — peligroso** |
| 4 | 🟠 | raíz | Sin CI. 291 tests que nadie ejecuta automáticamente; ruff y mypy instalados y no cableados | **ABIERTO** |
| 5 | 🟠 | `apps/exec/package.json` | Identidad de SDK público (`@catalyst-team/poly-sdk`, `private: false`, `publishConfig.access: public`, `prepublishOnly`) en el repo que contiene la wallet. Un `npm publish` accidental es posible |
| 6 | 🟠 | `apps/exec/src/services/` | ~7.500 LOC latentes (dip-arb 2.288, smart-money 2.277, arbitrage 1.857) heredadas de Bot1, fuera del flujo del bus, con sus tests de integración golpeando APIs reales |
| 7 | 🟠 | `orchestrator.py:75-78` | Los edges se evalúan en cascada `if None`: momentum solo se ve si overreaction calla. No es un ensemble, es una prioridad implícita — y produce el 78% del flujo |
| 8 | 🟡 | `README.md:31-36` | Tabla de fases dice Fase 2 y 3 "Pendiente"; ambas están commiteadas y la 3 lleva 15 días corriendo |
| 9 | 🟡 | `UmbraNoiti/` | 20.785 ficheros de copia pre-fusión en el árbol de trabajo, con su propio `.git` y `.venv` |
| 10 | 🟡 | `orchestrator.py:232` | `_liquidity_from_snapshots` usa `volume_24hr` como proxy de liquidez teniendo ya `bids`/`asks` reales del WS en el book cache |
| 11 | 🟡 | `risk/engine.py:152` | Import diferido de `portfolio.manager` dentro de la función para romper un ciclo. Funciona, señala un límite de módulos mal trazado |
| 12 | 🟡 | `scripts/health_check.py:24` | I001 de ruff preexistente (ya anotado en AUDIT_2026-07) |

---

## 6. Riesgos

**Operacional, activo ahora.** Redis no está escuchando en 6379. Postgres sí, en
5432, nativo (Docker Desktop está caído). Con Redis abajo el bus está muerto: el
outbox acumula, `exec` no cotiza, y —esto es lo que importa— `is_halted()` en
`mode != "live"` con `redis_fail_closed_in_sim = False` **devuelve `False` ante
un fallo de Redis**. En shadow eso es correcto por diseño y no toca dinero. En
`live` el fail-closed se activa solo. Verifica ese flag el día que cambies de
modo, porque es la diferencia entre pausar y seguir operando a ciegas.

**De medición.** El backtest sigue siendo optimista: no reproduce las compuertas
de riesgo con estado (posición abierta, exposición, caja, cooldown, frescura del
book), y ahora sabemos que además usa un modelo de coste 9,5× barato. Los dos
sesgos apuntan en la misma dirección: favorable.

**De validación.** El edge de overreaction nunca se ha validado y su `p_fair` no
está calibrado. El sistema lleva meses dimensionando con Kelly sobre una EMA
cruda. Kelly sobre probabilidades mal calibradas no es agresivo: es aleatorio.

**De concentración.** `momentum` genera el 78% del flujo y aparece en el roadmap
como algo que explícitamente *no* debía añadirse antes de validar overreaction
(`ROADMAP.md:23`). Entró igualmente, con default `True`.

**De supply chain.** `ethers 5` está en fin de vida y es la dependencia que
firma transacciones.

---

## 7. Sobre la arquitectura multi-exchange

Has pedido Adapters, Gateways y Plugin Pattern para no depender de Polymarket.
He medido el acoplamiento real antes de proponer nada: 37 de 74 ficheros
mencionan `condition_id`, pero código genuinamente específico de Polymarket solo
hay en `umbra/polymarket/` (client + schemas, ~2 ficheros).

Lo demás no es acoplamiento a *Polymarket*: es acoplamiento a **contratos
binarios con payout 1**. `sizer.py` asume pago de 1 USD por share; `paper.py`
distingue comprar NO de vender YES; los modelos hablan de `outcomes` y
`clob_token_ids`. Y resulta que Kalshi, Manifold y PredictIt son **todos**
binarios con payout 1. Esa supuesta deuda es, para tus tres exchanges
siguientes, un supuesto de dominio compartido y correcto.

La consecuencia es agradable: no necesitas Hexagonal. Necesitas dos puertos.

```python
class MarketDataSource(Protocol):        # lo que hoy hace polymarket/client.py
    async def list_markets(...) -> list[NormalizedMarket]: ...
    async def get_book(...) -> NormalizedBook: ...
    async def get_outcome(...) -> Outcome | None: ...

class ExecutionVenue(Protocol):          # lo que hoy hace el bus contra exec
    async def quote(intent) -> Quote: ...
    async def submit(intent) -> Fill: ...
```

`packages/contracts/` ya es, de hecho, la definición del segundo puerto: el bus
es la frontera de ejecución, y `exec` es ya un adaptador. Solo falta nombrarlo.
Coste estimado: 2-3 días cuando llegue el segundo exchange, no antes. Hacerlo hoy
sería diseñar una abstracción contra un solo caso conocido, que es la manera
fiable de acertar en lo que no importa.

**Recomendación: no toques esto todavía.** Es la propuesta que más te va a costar
aceptar y es la de mayor valor esperado.

---

## 8. Quick wins

Ordenados por (beneficio / esfuerzo), todos por debajo de medio día:

1. **Compuerta de nocional mínimo** en `risk/engine.py`. Un `min_notional_usd`
   (sugerencia inicial: $10, donde el p50 medido cae a 62 bps) evaluado *después*
   de los recortes 8-11. Elimina el 52% del flujo que no puede pagar su spread y
   el 100% de los rechazos de `exec`. Una compuerta, un test.
2. **Término de spread en el modelo de slippage.** `medio_spread + impacto`, con
   el spread que ya está en `BookSnapshot.spread`. Recalibra `slippage_base_bps`
   contra los 315 fills medidos.
3. **CI en GitHub Actions**: pytest + ruff + mypy + vitest. Todo está instalado y
   fijado; solo falta el YAML.
4. **`private: true`** en `apps/exec/package.json` y quitar `publishConfig`.
5. **Actualizar la tabla de fases del README** — dos líneas, y ahora mismo miente
   sobre el estado del sistema.
6. **Borrar o archivar `UmbraNoiti/`** — el propio `.gitignore` dice que el punto
   de retorno real de `exec` es `C:\Users\santi\Bot1`.

---

## 9. Roadmap priorizado

**~~Fase A — Cerrar el bucle de medición.~~ ✅ HECHA (28/07).** La divergencia
mediana quedó en −0,0 bps sobre 298 medidas fuera de muestra, muy por debajo del
criterio de salida que se había fijado (< 25 bps). El suelo de nocional está puesto.

**Fase B — Calibración de probabilidad (2-3 semanas). ← AQUÍ.** Cerrar GAP-01.
Sustituir el passthrough por calibración isotónica o Platt sobre los `outcomes`
resueltos. Hasta aquí, Kelly no tiene fundamento.

> **Criterio de salida corregido.** Decía «Brier out-of-sample < 0,20», y el propio
> 28 de julio demostró que ese umbral lo cumple un sistema sin edge. El criterio es
> ahora **batir al mercado con significancia**: IC 95% de la diferencia emparejada
> entero bajo cero, sobre ≥ 200 predicciones out-of-sample. Es exactamente lo que
> evalúa `MetricsReport.passes_acceptance`.

**Fase B′ — Medir el coste de salida (paralelizable con B).** `stage_intent` solo se
llama al abrir, así que el ida y vuelta es mitad medido y mitad supuesto. Sumar el
camino de cierre al bus, y un suelo de nocional en la salida — 3.173 de los cierres
movían menos de $1 cada uno.

**Fase C — Higiene estructural (1 semana, paralelizable).** Quick wins 3-6.
Decidir el destino de las 7.500 LOC latentes de `exec`: extraer a paquete propio
o borrar. Plan de migración `ethers 5 → 6`.

**Fase D — Honestidad del backtest (2 semanas).** Reproducir en el backtest las
compuertas con estado, con el modelo de coste ya corregido. Es lo que convierte
el gate §14 en un veredicto y no en una opinión.

**Fase E — Puertos multi-exchange.** Cuando exista un segundo exchange concreto
que integrar, y ni un día antes.

Fases A y B son secuenciales y bloquean todo lo demás. C es independiente. D
depende de A. E depende de una decisión de negocio, no de ingeniería.

---

## 10. Las tres propuestas, en tu formato

### P1 — Compuerta de nocional mínimo

**Problema.** 181 de 345 intents (52%) son de menos de $5 y pagan 281 bps; su
coste de ida y vuelta (562 bps) excede el retorno bruto de un edge en el umbral
(400 bps). Concentran el 100% de los rechazos de `exec`.
**Riesgo.** Bajo. Solo reduce el conjunto de órdenes aceptadas; ninguna orden
nueva se hace posible. Riesgo real: elegir un umbral demasiado alto y quedarse
sin muestra para validar. Mitiga empezando en $10 y midiendo.
**Beneficio.** Elimina el tramo de EV negativo estructural. Reduce el ruido en
todas las métricas agregadas, que hoy promedian órdenes que nunca debieron
existir.
**Alternativas.** (a) Subir `min_edge` — rechazado: no distingue una orden
pequeña con edge grande de una grande con edge pequeño. (b) Subir `κ` — peor:
aumenta varianza en todo el libro para arreglar la cola. (c) Umbral dinámico
`notional > spread_cost × k` — mejor a largo plazo, más difícil de calibrar hoy.
**Impacto.** El flujo de órdenes cae ~50% en número; el nocional agregado apenas
se mueve.
**Archivos.** `risk/engine.py` (compuerta 12), `config.py` (`min_notional_usd`),
`tests/test_risk_engine.py`.
**Compatibilidad.** Total. Configurable; a 0 el comportamiento es el actual.
**Prioridad.** P0. **Estimación.** 2 h.
**Migración.** Ninguna. Flag con default conservador, medir una ventana, ajustar.

### P2 — Término de spread en el modelo de slippage

**Problema.** `_slippage_bps` no conoce el spread, que es el coste dominante.
En producción devuelve la constante 20 bps. Error medido: +171 bps.
**Riesgo.** Medio. Cambia el coste de todos los backtests históricos: los
resultados previos dejan de ser comparables con los nuevos. Es el objetivo, pero
hay que anotarlo o alguien leerá una regresión donde hay una corrección.
**Beneficio.** El backtest empieza a medir la estrategia que se ejecuta. Sin
esto, la Fase D no tiene sentido.
**Alternativas.** (a) Caminar `bids`/`asks` del book cache — más exacto, y es a
donde hay que llegar; requiere que la profundidad esté siempre disponible, hoy no
lo está. (b) Recalibrar `slippage_base_bps` a 191 sin cambiar la forma —
rechazado: acierta la media y falla en toda la distribución, que es justo donde
el tamaño manda.
**Impacto.** Todas las métricas de rentabilidad empeoran. Correctamente.
**Archivos.** `execution/paper.py`, `backtest/engine.py`, `config.py`,
`tests/test_paper_execution.py`, `docs/AUDIT_*`.
**Compatibilidad.** Rompe comparabilidad histórica de backtests. No rompe API.
**Prioridad.** P0. **Estimación.** 1 día + medio de recalibración.
**Migración.** Implementar tras P1, revalidar contra los 315 fills, relanzar
shadow 7 días, comparar.

### P3 — CI

**Problema.** 290 tests de `brain` + 67 de `exec`, ruff y mypy fijados en
`requirements-dev.txt`, y nada de eso corre automáticamente. La suite ya tiene un
modo de fallo conocido y silencioso: sin `UMBRA_TEST_DATABASE_URL` da 16 fallos
que no son del proyecto.
**Riesgo.** Ninguno técnico. Coste real: el primer YAML expondrá fallos
preexistentes (el I001 de `health_check.py`, y probablemente mypy).
**Beneficio.** Los tests anti-lookahead dejan de depender de que alguien se
acuerde. Son los que impiden que el backtest mienta.
**Alternativas.** Pre-commit hooks — complementario, no sustituto: no protege
contra quien no los instala.
**Impacto.** Nulo en runtime.
**Archivos.** `.github/workflows/ci.yml` (nuevo), quizá `pyproject.toml`.
**Compatibilidad.** Total. **Prioridad.** P1. **Estimación.** 3 h.
**Migración.** Arrancar con pytest+vitest en modo bloqueante y ruff/mypy en modo
informativo; endurecer cuando la base esté limpia.

---

## Apéndice — Reproducir la medición

```bash
cd apps/brain
./.venv/Scripts/python.exe scripts/shadow_report.py --days 20
```

Requiere Postgres en 5432. Los datos van del 12 al 27 de julio de 2026,
`mode=shadow`, `DRY_RUN=true`, sin capital real en ningún momento.
