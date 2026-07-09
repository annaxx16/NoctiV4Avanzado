# umbraNocti — Documentación del proyecto

> Bot automatizado de trading sobre mercados de predicción (Polymarket), enfocado en capturar el edge de *overreaction*. Estado actual: paper trading, cero dinero real.

---

## Tabla de contenidos

1. [Qué es](#1-qué-es)
2. [Estado actual](#2-estado-actual)
3. [Stack técnico](#3-stack-técnico)
4. [Estructura del repositorio](#4-estructura-del-repositorio)
5. [Cómo viaja un dato — flujo end-to-end](#5-cómo-viaja-un-dato--flujo-end-to-end)
6. [Componentes en detalle](#6-componentes-en-detalle)
7. [Modelo de datos (tablas)](#7-modelo-de-datos-tablas)
8. [Endpoints de la API](#8-endpoints-de-la-api)
9. [Configuración (variables de entorno)](#9-configuración-variables-de-entorno)
10. [Tests](#10-tests)
11. [Limitaciones honestas](#11-limitaciones-honestas)
12. [Otros documentos](#12-otros-documentos)

---

## 1. Qué es

**umbraNocti** es un sistema que vigila Polymarket (mercados de predicción de eventos reales — política, deportes, cripto, etc.), detecta cuándo el precio se ha **alejado anormalmente** de su tendencia reciente (sobre-reacción), y genera **señales de apuesta paper** del lado contrario, asumiendo que el mercado revertirá.

La intuición: si el precio de "Cavaliers ganan" estaba estable en 40% durante 5 minutos y de repente salta a 55% sin razón aparente, probablemente esté sobre-reaccionando. El sistema apuesta a que volverá hacia 40%.

**Importante**: hoy es 100% simulación (modo `sim`). Genera señales, simula fills, lleva un portfolio virtual con $1,000 USD ficticios. Ningún dólar real se mueve.

---

## 2. Estado actual

Construido en 5 días según un plan de infraestructura. Lo que **sí** funciona:

- ✅ Descarga continua de Polymarket cada 30 s
- ✅ Persistencia en Postgres (Neon)
- ✅ Hot cache en Redis (Upstash)
- ✅ Cálculo de features con tests anti-lookahead
- ✅ Edge "OverreactionV1" basado en EMA + sigma threshold
- ✅ Risk engine con MIN_EDGE, MAX_RISK, exposure por mercado
- ✅ Position sizing Kelly fraccional κ=0.15
- ✅ Paper execution con modelo de slippage simple
- ✅ Portfolio tracking con PnL no-realizado
- ✅ Dashboard en Streamlit
- ✅ Kill-switch global
- ✅ 28 tests automáticos verdes

Lo que **NO** funciona aún (intencional):
- ❌ Trading con dinero real
- ❌ Walk-forward validation
- ❌ Calibración bayesiana de probabilidades
- ❌ Resolución de outcomes (PnL realizado siempre = 0)
- ❌ Otros 4 edges del plan original
- ❌ WebSocket de Polymarket (usamos polling REST)
- ❌ Alertas Telegram

Plan detallado de qué viene después: ver `ROADMAP.md`.

---

## 3. Stack técnico

| Capa | Tecnología | Por qué |
|---|---|---|
| Lenguaje | Python 3.11 | Async maduro, ecosistema científico |
| API web | FastAPI 0.115 + uvicorn | Async first, OpenAPI gratis |
| DB | Postgres 16 (Neon free tier) | Transaccional, SQL estándar |
| Cache / streams | Redis 7 (Upstash free tier) | Hot cache, kill-switch, signal stream |
| ORM | SQLAlchemy 2.0 async | Async + tipo-seguro |
| Migraciones | Alembic | Estándar SQLAlchemy |
| HTTP cliente | httpx async + tenacity | Retries exponenciales |
| Logging | structlog | JSON estructurado |
| Config | pydantic-settings | Por env vars |
| Tests | pytest + pytest-asyncio | Sync + async |
| Dashboard | Streamlit + pandas | UI rápida sin frontend |

---

## 4. Estructura del repositorio

```
umbraNocti/
├── .env                    # credenciales reales (NO commitear)
├── .env.example            # template
├── .gitignore
├── alembic.ini             # config Alembic
├── alembic/                # migraciones DB
│   ├── env.py
│   └── versions/           # 3 migraciones aplicadas
├── dashboard/
│   └── app.py              # Streamlit cockpit
├── docker-compose.yml      # opcional (si quieres Postgres/Redis local)
├── infra/
│   ├── smoke.py            # smoke test de conectividad Postgres + Redis
│   └── check_tables.py     # lista tablas en la DB
├── scripts/
│   ├── run_api.py          # entry point de la API (fix event loop Windows)
│   ├── start-api.ps1       # wrapper que evita problemas con Activate.ps1
│   └── start-dashboard.ps1
├── src/umbra/              # paquete principal
│   ├── __init__.py         # __version__ = "0.1.0"
│   ├── api/                # FastAPI endpoints
│   ├── cache/              # Redis client + hot book cache
│   ├── config.py           # Settings vía pydantic-settings
│   ├── db/                 # SQLAlchemy: base, models, session
│   ├── edges/              # OverreactionV1
│   ├── engine/             # probability engine + orchestrator
│   ├── execution/          # paper trading + modelo slippage
│   ├── features/           # calculator (puro) + loader (desde DB)
│   ├── logging.py          # structlog setup
│   ├── polymarket/         # cliente async Gamma + schemas Pydantic
│   ├── portfolio/          # equity, PnL no-realizado
│   ├── risk/               # risk engine + Kelly sizer
│   ├── scheduler/          # background tasks: scanner + poller
│   └── universe/           # filtrado del top-N de mercados
├── tests/                  # 28 tests, todos verdes
│   ├── conftest.py         # fix event loop policy Windows
│   ├── leakage/            # anti-lookahead obligatorio
│   ├── test_gamma_client.py
│   ├── test_health.py
│   ├── test_orchestrator_e2e.py
│   ├── test_orchestrator_paper.py
│   ├── test_overreaction.py
│   ├── test_paper_execution.py
│   └── test_sizer.py
├── pyproject.toml          # paquete `umbra` editable
├── requirements.txt        # deps pinneadas
├── README.md               # quickstart
├── OPERATIONS.md           # cómo operar día a día
├── ROADMAP.md              # plan post-Día 5
└── DOCUMENTATION.md        # este archivo
```

---

## 5. Cómo viaja un dato — flujo end-to-end

```
   Polymarket Gamma API (REST público)
            │
            │  cada 30s
            ▼
   ┌────────────────────┐
   │   poller (async)   │
   └────────────────────┘
            │
            ├──► PostgreSQL: INSERT en book_snapshots
            │
            ├──► Redis:      SET book:{condition_id} (TTL 60s)
            │
            ▼
   ┌────────────────────┐
   │     orchestrator   │
   └────────────────────┘
            │
            │  1. load_snapshots(últimos 30 min)
            ▼
   ┌────────────────────┐
   │  feature calculator│  → mid_price, spread, Δp 1m/5m, vol_z…
   └────────────────────┘
            │
            ▼
   ┌────────────────────┐
   │ overreaction edge  │  → EMA(mid) vs market_price, sigma>3?
   └────────────────────┘
            │
            │  Si sigma >= 3:
            ▼
   ┌────────────────────┐
   │ probability engine │  → p_fair = EMA(mid)
   └────────────────────┘
            │
            ▼
   ┌────────────────────┐
   │ Kelly sizer (κ.15) │  → shares + notional_usd
   └────────────────────┘
            │
            ▼
   ┌────────────────────┐
   │   risk engine      │  → ¿kill-switch? ¿MIN_EDGE? ¿MAX_RISK?
   └────────────────────┘
            │
            │  Si accepted:
            ▼
   ┌────────────────────┐
   │  paper execution   │  → calcula slippage, inserta PaperFill,
   │                    │    upserta PaperPosition
   └────────────────────┘
            │
            ├──► PostgreSQL: INSERT signals, fills_paper, portfolio_state
            │
            └──► Redis:      XADD umbra:signals (stream auditado)

   Dashboard Streamlit lee de la API ──► muestra al usuario
```

Cada **30 segundos**, este ciclo completo se ejecuta para cada uno de los 20 mercados del universo activo.

Cada **5 minutos**, el universe scanner refresca esos 20 mercados.

---

## 6. Componentes en detalle

### 6.1. Polymarket client (`src/umbra/polymarket/`)

- **`client.py`**: `GammaClient` async basado en httpx. Métodos `list_markets()`, `iter_markets()`, `get_market_by_condition_id()`. Retries con backoff exponencial (Tenacity).
- **`schemas.py`**: `GammaMarket` Pydantic con validadores que parsean strings JSON anidados (Polymarket devuelve `outcomes` y `outcomePrices` como strings JSON en la respuesta).

### 6.2. Universe scanner (`src/umbra/universe/scanner.py`)

Cada 5 min:
1. Llama Gamma para los mercados ordenados por volumen 24h.
2. Filtra: `active=true`, `closed=false`, `accepting_orders=true`, liquidez ≥ $5,000, volumen 24h ≥ $1,000.
3. Toma los top-20.
4. Upserta a la tabla `markets` (metadata estable) y refresca completamente `markets_active`.

### 6.3. Poller (`src/umbra/scheduler/poller.py`)

Cada 30 s:
1. Lee `markets_active`.
2. Para cada mercado, llama `get_market_by_condition_id()`.
3. Persiste un nuevo `BookSnapshot`.
4. Actualiza el hot cache Redis (`book:{condition_id}`, TTL 60s).
5. Llama al orchestrator para cada mercado.

### 6.4. Feature calculator (`src/umbra/features/calculator.py`)

Función pura: recibe lista de `SnapshotInput` ordenados + `as_of` (timestamp), devuelve `FeatureSet`. Features:

| Feature | Cálculo | Para qué |
|---|---|---|
| `mid_price` | `(bid + ask) / 2` | Precio "consensus" actual |
| `spread` | `ask - bid` | Cuán líquido es el mercado |
| `delta_p_1m` | `mid(t) - mid(t-1m)` | Velocidad de corto plazo |
| `delta_p_5m` | `mid(t) - mid(t-5m)` | Tendencia de mediano plazo |
| `spread_expansion` | z-score del spread vs últimos 5min | Detecta volatilidad anómala |
| `vol_z` | z-score del volumen 24h vs últimos 30 min | Detecta interés repentino |
| `mid_velocity` | `(mid(t) - mid(t-1)) / dt` | Derivada simple del precio |

Tests anti-lookahead (`tests/leakage/`) verifican que **ningún cálculo usa snapshots con ts > as_of**.

### 6.5. Edge OverreactionV1 (`src/umbra/edges/overreaction.py`)

Algoritmo:
1. Requiere ≥11 snapshots históricos.
2. Calcula `fair_price = EMA(mid_history, alpha=0.1)` sobre los snapshots **anteriores** al actual (no incluye el actual).
3. Calcula `recent_std = stdev(últimos 10 mids anteriores)`.
4. `sigma = (market_price - fair_price) / recent_std`
5. Si `|sigma| < 3` → no hay señal.
6. Si `sigma > +3` → mercado sobre-reaccionó al alza → `BUY_NO` (apostar que baja).
7. Si `sigma < -3` → sobre-reaccionó a la baja → `BUY_YES` (apostar que sube).

Por qué excluimos el actual del cálculo de std: si lo incluyéramos, la propia magnitud del spike inflaría el std y enmascararía la sobre-reacción.

### 6.6. Probability engine (`src/umbra/engine/probability.py`)

Hoy es **passthrough**: `p_fair = fair_price del edge`. En el futuro (post-D5, ver ROADMAP) aplicará calibración isotónica/bayesiana sobre histórico real.

### 6.7. Kelly fractional sizer (`src/umbra/risk/sizer.py`)

Para apuesta binaria con precio de mercado `p_m` y probabilidad estimada `p_f`:

```
b = (1 - p_m) / p_m            # odds
f* = (p_f * b - (1 - p_f)) / b  # Kelly óptimo
size_usd = κ * bankroll * f*    # fractional Kelly
shares = size_usd / p_m
```

κ = 0.15 (15% del Kelly completo) reduce varianza significativamente a cambio de menor retorno teórico. Es lo que la literatura recomienda para retail.

Si `f* < 0` (mercado dice más probable que nuestra estimación), no apostamos: tamaño = 0.

### 6.8. Risk engine (`src/umbra/risk/engine.py`)

Chequeos en orden, cualquiera que falle rechaza la señal:

1. **Kill-switch**: si `umbra:halt` está en Redis con valor `"1"`, rechazar todo.
2. **MIN_EDGE**: si `|fair - market_price| < 0.02` (2pp), rechazar.
3. **Kelly cero**: si el sizer devolvió 0 shares, rechazar.
4. **MAX_RISK_PER_TRADE**: si notional > $50, recortar al cap.
5. **MAX_EXPOSURE_PER_MARKET**: si suma de notionals aceptados en este mercado + nuevo > $200, recortar.

Cada decisión queda registrada en `Signal.reason`.

### 6.9. Paper execution (`src/umbra/execution/paper.py`)

Modelo de slippage simple:

```
slippage_bps = base_bps + size_factor_bps * (notional / liquidity)
              capeado a 500 bps (5%)
```

Default: base = 20 bps, size_factor = 200 bps. Si compras $10 en un mercado de $10,000 de liquidez (ratio 0.001), slippage ≈ 20.2 bps.

`fill_price = teórico * (1 + slippage_bps/10_000)`. Para `BUY_YES`, teórico = `mid_yes`; para `BUY_NO`, teórico = `1 - mid_yes`.

Cada fill genera 1 fila en `fills_paper` y upsertea (o crea) 1 fila en `portfolio_state` con shares acumuladas y avg_entry_price ponderado.

### 6.10. Portfolio manager (`src/umbra/portfolio/manager.py`)

- `portfolio_snapshot()`: equity actual = cash + valor de mercado de posiciones.
- `position_views()`: cada posición con PnL no-realizado calculado contra el precio actual del cache.
- `equity_curve()`: serie temporal a partir de fills (cost-basis, no marca a mercado).

### 6.11. Dashboard (`dashboard/app.py`)

Streamlit consume la API local. Componentes:
- Botones halt/resume.
- 5 KPI cards (equity, PnL, posiciones, señales, aceptadas).
- Equity curve.
- Tabla de posiciones abiertas.
- Tabla de últimas 50 señales (incluyendo rechazadas con razón).
- Tabla de últimos 50 fills paper.
- Tabla del universo activo.

---

## 7. Modelo de datos (tablas)

### `markets`
Metadata estable. Una fila por condition_id.
```
condition_id (PK), gamma_id, slug, question, clob_token_ids[],
outcomes[], end_date, start_date, first_seen_at, last_seen_at
```

### `book_snapshots`
Serie temporal. Una fila por cada poll por cada mercado.
```
id (PK), market_id (FK), ts, best_bid, best_ask, last_trade_price,
spread, liquidity_num, volume_24hr, active, accepting_orders
```

### `markets_active`
Universo actual. Sin historial — refrescado en cada scan.
```
condition_id (PK + FK), rank, liquidity_num, volume_24hr, selected_at
```

### `signals`
Cada evaluación del edge (aceptada o rechazada).
```
id (PK), ts, market_id (FK), edge_name, side,
market_price, fair_price, edge_value, strength,
size_shares, notional_usd, accepted, reason, mode
```

### `fills_paper`
Cada fill simulado.
```
id (PK), ts, signal_id (FK), market_id (FK), side,
shares, mid_at_fill, fill_price, slippage_bps,
notional_usd, fees_usd, mode
```

### `portfolio_state`
Posiciones acumuladas. PK compuesta (market_id, side).
```
market_id (PK + FK), side (PK), opened_at, last_updated_at,
shares, avg_entry_price, total_cost_usd, total_fees_usd,
n_fills, status
```

---

## 8. Endpoints de la API

Base: `http://127.0.0.1:8000`

### Salud y meta
- `GET /health` → `{"status": "ok"}`
- `GET /version` → `{"version": "0.1.0", "mode": "sim"}`
- `GET /stats` → contadores agregados de tablas

### Datos de mercado
- `GET /universe` → top 20 mercados activos
- `GET /markets/{condition_id}/features?as_of=ISO8601` → feature vector
- `GET /markets/{condition_id}/book` → último book desde el hot cache Redis

### Señales
- `GET /signals?limit=20&accepted_only=false` → últimas señales

### Portfolio
- `GET /portfolio` → snapshot completo (cash, equity, posiciones)
- `GET /portfolio/equity-curve?hours=24` → puntos de la curva
- `GET /fills?limit=50` → fills paper recientes

### Admin
- `POST /admin/halt` → activa kill-switch
- `POST /admin/resume` → desactiva kill-switch

Documentación interactiva: `http://127.0.0.1:8000/docs` (Swagger UI autogenerado por FastAPI).

---

## 9. Configuración (variables de entorno)

Archivo `.env` (basado en `.env.example`):

| Variable | Default | Para qué |
|---|---|---|
| `DATABASE_URL` | localhost dev | Connection string Postgres (debe empezar con `postgresql+psycopg://`) |
| `REDIS_URL` | localhost dev | Connection string Redis (Upstash requiere `rediss://`) |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `MODE` | `sim` | `sim` / `paper` / `live` (live no implementado aún) |
| `POLYMARKET_GAMMA_URL` | `https://gamma-api.polymarket.com` | Override si Polymarket cambia URL |
| `MIN_LIQUIDITY_USD` | `5000` | Filtro mínimo para el universo |
| `MIN_VOLUME_24H_USD` | `1000` | Filtro mínimo para el universo |
| `UNIVERSE_TOP_N` | `20` | Cuántos mercados tradear |
| `UNIVERSE_SCAN_INTERVAL_SEC` | `300` | Cada cuánto refrescar universo |
| `POLL_INTERVAL_SEC` | `30` | Cada cuánto pollear cada mercado |
| `BANKROLL_USD` | `1000` | Bankroll inicial paper |
| `KELLY_KAPPA` | `0.15` | Fracción de Kelly óptimo a usar |
| `MIN_EDGE` | `0.02` | Edge mínimo en probabilidad (2pp) |
| `MAX_RISK_PER_TRADE_USD` | `50` | Cap por trade |
| `MAX_EXPOSURE_PER_MARKET_USD` | `200` | Cap acumulado por mercado |
| `OVERREACTION_SIGMA_THRESHOLD` | `3.0` | Umbral para disparar señal |
| `OVERREACTION_MIN_SNAPSHOTS` | `10` | Histórico mínimo para evaluar |
| `EMA_ALPHA` | `0.1` | Suavizado del fair price (más bajo = más suave) |
| `SLIPPAGE_BASE_BPS` | `20.0` | Slippage base |
| `SLIPPAGE_SIZE_FACTOR_BPS` | `200.0` | Slippage adicional por unidad de notional/liquidez |
| `SLIPPAGE_CAP_BPS` | `500.0` | Slippage máximo permitido |
| `FEE_BPS` | `0.0` | Fees Polymarket (hoy 0% en muchos mercados) |

Todos los settings están definidos en `src/umbra/config.py`.

---

## 10. Tests

28 tests automáticos en `tests/`:

| Archivo | Qué prueba |
|---|---|
| `leakage/test_no_lookahead.py` | 6 tests: features nunca usan datos futuros |
| `test_gamma_client.py` | 2 tests integración: descarga real de Polymarket |
| `test_health.py` | 2 tests: `/health` y `/version` |
| `test_overreaction.py` | 5 tests: edge detecta spikes y no falsos positivos |
| `test_sizer.py` | 5 tests: matemática del Kelly fractional |
| `test_paper_execution.py` | 6 tests: slippage, fill price |
| `test_orchestrator_e2e.py` | 1 test E2E: snapshots sintéticos → Signal aceptada |
| `test_orchestrator_paper.py` | 1 test E2E: Signal → PaperFill → PaperPosition |

Ejecutar:
```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

---

## 11. Limitaciones honestas

Lo que **NO** podemos prometer hoy:

1. **No es prueba de que ganarás dinero.** Es infraestructura para experimentar. La mayoría de bots retail no ganan plata. El edge tiene que validarse con meses de datos antes de pensar en live.

2. **PnL realizado es cero siempre.** No cerramos posiciones ni resolvemos outcomes. Hasta que un mercado expire, el PnL es marca a mercado del momento — útil para visualizar, no para reclamar resultados.

3. **Slippage es heurística.** El modelo no usa el order book real. Para slippage realista, hay que pasar a la CLOB API (en el roadmap).

4. **Brier / win rate no son válidos todavía.** Necesitan outcomes resueltos. Ver D8 en `ROADMAP.md`.

5. **No hay alertas.** Si la API crashea, te enteras al refrescar el dashboard.

6. **Dependemos de Polymarket Gamma.** Si cambian schema o agregan rate limits, hay que adaptar.

7. **Free tier de Neon / Upstash tiene límites.** Cuando el histórico crezca a >500 MB de snapshots, hay que migrar a paid tier o cleanear.

---

## 12. Otros documentos

- **`README.md`**: quickstart de instalación.
- **`OPERATIONS.md`**: guía operativa día a día — cómo arrancar/parar/leer el dashboard/resolver errores comunes.
- **`ROADMAP.md`**: qué viene después del Día 5 — backtesting, calibración, edges adicionales, camino a dinero real.
- **`DOCUMENTATION.md`** (este archivo): arquitectura, componentes, modelo de datos, decisiones técnicas.

Si algo no está claro o ves algo que esta documentación no cubra, vale la pena anotarlo para arreglarlo en la próxima sesión.
