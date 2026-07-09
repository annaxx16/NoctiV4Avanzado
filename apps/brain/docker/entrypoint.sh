#!/usr/bin/env bash
# Arranque del rol API: aplica migraciones y levanta uvicorn en 0.0.0.0.
# En Linux no hace falta el hack del event loop de Windows (sys.platform != win32).
set -euo pipefail

echo "[entrypoint] alembic upgrade head ..."
alembic upgrade head

echo "[entrypoint] uvicorn umbra.api.app:app en 0.0.0.0:8000 ..."
exec uvicorn umbra.api.app:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-level "${UVICORN_LOG_LEVEL:-warning}"
