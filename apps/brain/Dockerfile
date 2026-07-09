# umbraNocti — imagen única para API+jobs y dashboard (el comando decide el rol).
# Python 3.11 fijo (el proyecto está pineado a >=3.11,<3.12).
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl para los healthchecks de compose; nada más (psycopg[binary] trae su libpq).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Capa de dependencias (cacheable): solo requirements.txt cambia poco.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Código de la app.
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY alembic ./alembic
COPY dashboard ./dashboard
COPY scripts ./scripts
COPY infra ./infra
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN pip install -e . && chmod +x /usr/local/bin/entrypoint.sh

# Usuario no-root.
RUN useradd --create-home --uid 10001 umbra && chown -R umbra:umbra /app
USER umbra

EXPOSE 8000 8501

# Rol por defecto = API (migra + uvicorn). El dashboard sobreescribe el command.
CMD ["entrypoint.sh"]
