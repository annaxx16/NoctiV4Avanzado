# umbraNocti

Bot de trading sobre Polymarket — edge de overreaction. Paper trading first.

## Estado

**Día 1**: infraestructura base. No opera mercados todavía. No mueve dinero.

## Requisitos

- Python 3.11
- Git
- Postgres 16 + Redis 7 (cloud o local — ver abajo)

## Backend de datos: dos opciones

### Opción A — Cloud free tier (recomendada para empezar)

- **Postgres**: [Neon](https://console.neon.tech) — free tier 0.5 GB
- **Redis**: [Upstash](https://console.upstash.com) — free tier 256 MB / 10k cmd/día

Crear los recursos, copiar las URLs y pegarlas en `.env`:

```
DATABASE_URL=postgresql+psycopg://usuario:password@ep-xxxxx.aws.neon.tech/neondb?sslmode=require
REDIS_URL=rediss://default:TOKEN@xxxxx.upstash.io:6379
```

### Opción B — Docker local

Si más adelante tienes Docker Desktop funcionando:

```powershell
docker compose up -d
docker compose ps   # ambos deben estar healthy
```

Y en `.env` dejas los valores por defecto (localhost).

## Levantar la app (Windows / PowerShell)

```powershell
# 1. Situarse en la carpeta
Set-Location "C:\Users\Karemcita linda\umbraNocti"

# 2. Copiar variables de entorno y editarlas
Copy-Item .env.example .env
# (editar .env con las URLs reales de Neon + Upstash o dejar localhost si usas Docker)

# 3. Crear venv con Python 3.11 e instalar deps
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

# 4. Aplicar migraciones a la DB
alembic upgrade head

# 5. Arrancar la API
#    En Windows usa el script (fix de event loop para psycopg async).
#    En Linux/Mac, uvicorn directo funciona.
python scripts/run_api.py
```

En otra terminal:

```powershell
curl http://localhost:8000/health
# {"status":"ok"}

curl http://localhost:8000/version
# {"version":"0.1.0","mode":"sim"}
```

## Tests

```powershell
.\.venv\Scripts\Activate.ps1
pytest
```

## Parar todo

```powershell
docker compose down          # detiene servicios (mantiene datos)
docker compose down -v       # detiene y BORRA volúmenes (cuidado)
```

## Estructura

```
umbraNocti/
├── docker-compose.yml      # Postgres 16 + Redis 7
├── pyproject.toml          # paquete umbra (src layout)
├── requirements.txt        # deps pinneadas
├── src/umbra/
│   ├── api/app.py          # FastAPI: /health, /version
│   ├── config.py           # pydantic-settings
│   └── logging.py          # structlog JSON
└── tests/
    └── test_health.py
```

## Modos

Variable `MODE` en `.env`:
- `sim`  — solo genera señales, nunca ejecuta (default Día 1-4)
- `paper` — simula fills contra book real (Día 5)
- `live` — dinero real (no implementado; requiere validación previa de semanas)

## Próximos días

- D2: cliente Polymarket REST + persistencia
- D3: feature engine + Redis hot cache + DuckDB
- D4: edge Overreaction + Risk Engine + kill-switch
- D5: paper trading + dashboard Streamlit
