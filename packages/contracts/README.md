# packages/contracts — el contrato del bus

Fuente única de verdad de lo que `brain` (Python) y `exec` (Node) se dicen por Redis.
Si un campo cambia aquí, cambia en los dos lados o el CI falla.

## Los tres canales

| Canal | Tipo Redis | Productor | Consumidor | Fase |
|---|---|---|---|---|
| `nocti:universe` | `SET` con TTL (4 escaneos) | brain | exec | 1 |
| `book:{condition_id}` | `SET` con TTL 60s | exec | brain | 1 |
| `nocti:intents` | Stream + consumer group `exec` | brain | exec | 3 |
| `nocti:fills` | Stream + consumer group `brain` | exec | brain | 3 |
| `umbra:halt` / `umbra:halt:reason` | `SET` | ambos | ambos | 0 (ya existe) |

Streams, no pub/sub, para intents y fills. Pub/sub pierde mensajes si el consumidor
está caído, y aquí los mensajes son órdenes.

**exec no habla con Postgres.** No tiene `DATABASE_URL` ni le hace falta. brain es el
dueño de la contabilidad y le pasa por `nocti:universe` lo único que exec necesita
saber: qué mercados vigilar, sus `token_ids`, y la liquidez/volumen que da Gamma y el
WebSocket no. Menos superficie, y los secretos de la base de datos en un solo sitio.

**`yes_token_id` viene resuelto, no se adivina.** Gamma reporta `bestBid`/`bestAsk` a
nivel de mercado refiriéndose al lado YES, y todo brain lo asume. Deducirlo de
`token_ids[0]` funciona casi siempre; cuando no, exec publicaría el libro del NO como
si fuera el del mercado y brain vería **todos los precios invertidos**, sin que nada
fallara. brain conoce `outcomes` y lo resuelve; exec no adivina. Si no hay un YES
identificable, ese mercado no se vigila: mejor un hueco que un precio invertido.

El TTL de `nocti:universe` es deliberado. Si brain muere, el universo caduca, exec se
desuscribe y deja de publicar; los books caducan a los 60s; cuando brain vuelve, su
poller no encuentra nada fresco y cae a Gamma por su cuenta. El sistema degrada solo.

## Reglas que no se negocian

**1. Decimales como string.** Ningún `size_usd`, `price` o `pnl` viaja como float
JSON. Se serializan como string y se parsean a `Decimal` (Python) o al tipo decimal
correspondiente (TS). Un float de 64 bits no representa `0.62` exactamente, y aquí
eso es dinero.

**2. Idempotencia por `intent_id`.** Antes de firmar, exec ejecuta:

```
SET nocti:intent:{intent_id} 1 NX EX 86400
```

Si la clave ya existía, el intent ya se procesó: exec lo descarta y re-emite el fill
previo. Sin esto, un restart de brain reenvía los intents no-ackeados del stream y
**exec firma dos veces**. Es el fallo más caro que permite esta arquitectura.

**3. Halt fail-closed y simétrico.** exec lee `umbra:halt` antes de cada firma.
Si vale `"1"`, no firma. **Si Redis no responde, tampoco firma.** brain ya se
comporta así (`src/umbra/risk/engine.py:66`). Si exec detecta un fallo grave de
ejecución, escribe `umbra:halt` y `umbra:halt:reason`.

**4. exec nunca dimensiona.** El `size_usd` viene calculado por el risk engine de
brain. exec no lo aumenta, no lo redondea al alza, y rechaza cualquier intent que
no haya pasado por ahí. Un solo presupuesto de capital, una sola wallet.

## Compatibilidad hacia atrás

`book:{condition_id}` ya lo lee brain hoy (`src/umbra/cache/book_cache.py`), poblado
por su propio poller REST. exec escribe **exactamente la misma forma**, más dos campos
opcionales (`bids`, `asks`). Los lectores viejos ignoran lo que no conocen; los nuevos
usan la profundidad real del book en vez de `volume_24hr` como proxy de liquidez.

Ese proxy está en `src/umbra/execution/paper.py:47` y es la razón por la que hoy
ningún número de rentabilidad del backtest es comprobable.
