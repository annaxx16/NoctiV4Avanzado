# umbraNocti — Manual de operación

Este documento describe cómo arrancar, parar, monitorear y resolver problemas comunes del sistema en su estado del Día 5.

## TL;DR

```powershell
Set-Location "C:\Users\Karemcita linda\umbraNocti"

# Terminal 1: API + background jobs
.\scripts\start-api.ps1

# Terminal 2: dashboard
.\scripts\start-dashboard.ps1
```

Después abrir `http://localhost:8501` en el navegador.

> **NO uses** `python scripts/run_api.py` directamente — el `python` del sistema apunta a 3.14 que no tiene las deps. Los scripts `.ps1` arriba usan la ruta absoluta al Python 3.11 del venv y son a prueba de errores.

---

## Despliegue con Docker (recomendado para servidor)

Todo el stack (Postgres + Redis + API/jobs + dashboard) corre con un solo comando.
Requisitos en la máquina destino: Docker Engine + plugin compose (v2.24+).

```bash
# 1. Subir el proyecto (git clone o rsync) y entrar al directorio
# 2. Configurar secretos (opcional pero recomendado):
cp .env.example .env
#    - ADMIN_TOKEN: python3 -c "import secrets;print(secrets.token_urlsafe(32))"
#    - MODE=sim (default) — no tocar hasta validar el edge
#    NO toques DATABASE_URL/REDIS_URL: el compose los inyecta apuntando a sus servicios.

# 3. Construir y arrancar todo
docker compose up -d --build

# 4. Verificar
docker compose ps                       # los 4 servicios "healthy"/"running"
curl -s http://127.0.0.1:8000/health    # {"status": "ok"}
docker compose logs -f app              # logs JSON del bot
```

Qué hace el arranque: el contenedor `app` espera a que Postgres/Redis estén
healthy, aplica `alembic upgrade head` automáticamente (ver
`docker/entrypoint.sh`) y levanta la API con todos los background loops
(scanner, poller, exits, equity, outcomes, supervisor). El dashboard espera a
que la API esté healthy.

**Seguridad por defecto**: todos los puertos (5432, 6379, 8000, 8501) están
bindeados a `127.0.0.1` del host — nada queda expuesto a internet. Para ver el
dashboard desde tu máquina:

```bash
ssh -L 8501:localhost:8501 -L 8000:localhost:8000 usuario@servidor
# luego abrir http://localhost:8501
```

**Operación diaria**:

```bash
docker compose logs -f app                  # seguir logs
docker compose restart app                  # reiniciar solo el bot
docker compose down                         # parar todo (los datos persisten en volúmenes)
docker compose down -v                      # ⚠️ parar Y BORRAR datos (pierdes los snapshots)
docker compose up -d --build                # redeploy tras cambios de código
```

**Backup de datos** (los snapshots acumulados son el activo crítico — sin ellos
no hay `FINDINGS_W1.md`):

```bash
docker exec umbra_postgres pg_dump -U umbra umbra | gzip > umbra_$(date +%F).sql.gz
```

Ponlo en un cron diario en el servidor.

---

## Estado actual del sistema

- **Modo**: `sim` (no toca dinero real, jamás)
- **Datos**: Polymarket Gamma API (polling cada 30 s)
- **Storage**: Neon Postgres (transaccional) + Upstash Redis (hot cache + streams)
- **Edge activo**: OverreactionV1 (EMA + 3σ threshold)
- **Sizing**: Kelly fraccional κ=0.15 sobre bankroll $1,000 simulado

---

## Arrancar todo

### 1. Configurar entorno (sólo primera vez)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

Copy-Item .env.example .env
# editar .env con las URLs reales de Neon + Upstash
```

### 2. Verificar conectividad

```powershell
python infra/smoke.py
```

Espera dos líneas con `"ok": true`. Si falla:
- Postgres: revisar que `DATABASE_URL` empiece por `postgresql+psycopg://` y termine con `?sslmode=require&channel_binding=require`
- Redis: revisar que use `rediss://` (doble S, TLS obligatorio para Upstash)

### 3. Aplicar migraciones (sólo cuando haya cambios de schema)

```powershell
alembic upgrade head
```

### 4. Arrancar la API

```powershell
python scripts/run_api.py
```

Esto inicia:
- FastAPI en `http://127.0.0.1:8000`
- Universe scanner (cada 5 min)
- Poller (cada 30 s) → snapshots + features + edge + risk + paper fills

### 5. Arrancar el dashboard

En otra ventana:

```powershell
.\.venv\Scripts\Activate.ps1
streamlit run dashboard/app.py
```

Abre automáticamente `http://localhost:8501`.

---

## Parar todo

- **API**: `Ctrl+C` en la terminal donde corre `run_api.py`. El lifespan limpia background tasks, sesiones y Redis.
- **Dashboard**: `Ctrl+C` en la terminal de Streamlit.
- **No hace falta parar Neon ni Upstash** (son servicios externos).

---

## Cómo leer el dashboard

Layout de arriba a abajo:

### Botones superiores
- **Refresh**: vuelve a consultar el API.
- **Halt** / **Resume**: kill-switch global. Halt activa la key Redis `umbra:halt`. Si está activa, el risk engine RECHAZA toda señal nueva. Resume la borra.

### KPIs (5 tarjetas)
- **Equity (USD)**: cash + valor de mercado de posiciones abiertas. Empieza en $1,000.
- **Unrealized PnL**: diferencia entre el costo de las posiciones y su valor de mercado actual.
- **Open positions**: número de `(market, side)` con shares > 0.
- **Signals total**: total histórico de señales (aceptadas + rechazadas).
- **Signals accepted**: cuántas pasaron el risk engine.

### Equity curve (cost basis)
- Eje X: tiempo. Eje Y: dinero gastado acumulado.
- Esta NO es la equity real porque muestra cost-basis (no marca a mercado en cada punto). Es útil para ver la frecuencia y tamaño de los trades.

### Tabla "Posiciones abiertas"
- `avg_entry_price`: precio promedio ponderado de entrada (incluyendo slippage).
- `current_price`: precio actual del lado (YES o NO según side).
- `unrealized_pnl_usd = shares * (current_price - avg_entry_price)`.

### Tabla "Últimas señales"
- Las señales rechazadas también aparecen, con su `reason`.
- `strength`: número de sigmas del overreaction. `> +3` o `< -3` para generarse.

### Tabla "Últimos fills paper"
- `slippage_bps`: cuánto pagamos de más por slippage (modelo simple proporcional a notional/liquidez).
- `mid_at_fill`: precio teórico al momento del fill (antes de slippage).

### Tabla "Universo activo"
- Los top-20 mercados elegidos por liquidez + volumen 24h.
- Se refresca cada 5 min.

---

## Problemas comunes

### "El dashboard no se conecta"
Verificar que `python scripts/run_api.py` esté corriendo. Probar manualmente:
```powershell
Invoke-WebRequest http://127.0.0.1:8000/health
```

### "No aparecen señales"
- Esperar al menos 10 ticks (5 minutos) para que cada mercado tenga histórico suficiente.
- Las señales reales con overreaction >3σ son raras en mercados líquidos. Para validar end-to-end:
  ```powershell
  python -m pytest tests/test_orchestrator_paper.py -v
  ```

### "psycopg.InterfaceError: ProactorEventLoop"
Estás corriendo uvicorn directamente. **Usa `python scripts/run_api.py`**, que setea `WindowsSelectorEventLoopPolicy` antes de crear el loop.

### "El polling se quedó atascado / la API responde lento"
- Revisar logs de la API (terminal donde corre `run_api.py`) — son JSON estructurados.
- Si Neon o Upstash tienen latencia alta (típico al despertar de free tier), el primer tick post-arranque puede tardar 5–10 s.

---

## Datos y limpieza

- **Borrar todo el histórico**: `alembic downgrade base; alembic upgrade head`. Cuidado: pierdes todas las observaciones.
- **Resetear bankroll paper**: por ahora bastará borrar `fills_paper` y `portfolio_state`:
  ```sql
  TRUNCATE fills_paper, portfolio_state;
  ```
- **Rotar credenciales**: dashboards de Neon y Upstash → reset password → actualizar `.env` → reiniciar API.

---

## Lo que NO está en este Día 5 (importante)

Estas son limitaciones conscientes:

- **Cero dinero real** — modo `sim` siempre por ahora. No hay code path para enviar órdenes a Polymarket aún.
- **PnL no realizado solo** — no cerramos posiciones ni resolvemos outcomes. Hasta que un mercado resuelva, el PnL es marca a mercado.
- **Slippage es heurística simple** — `base_bps + size_factor_bps * (notional/liquidity)`, capeado. No modela order book real.
- **Métricas de calidad (Brier, win rate) son N/A** — requieren outcomes resueltos. Vienen en el roadmap.
- **No hay alertas** — sin email, Slack, Telegram. Si el sistema crashea, te enteras al refrescar el dashboard.
