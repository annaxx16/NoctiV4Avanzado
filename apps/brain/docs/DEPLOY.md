# Despliegue en VPS Linux (Docker, self-contained)

Stack completo en contenedores: **Postgres + Redis + API (con jobs) + dashboard**.
Sin servicios externos: todo el estado vive en volúmenes Docker del propio VPS.
Modo `sim` (no toca dinero real).

Artefactos: `Dockerfile`, `docker/entrypoint.sh`, `docker-compose.prod.yml`,
`.env.prod.example`.

---

## 0. Requisitos del servidor (una vez)

VPS Linux (Ubuntu 22.04+/Debian 12+ recomendado, ≥1 vCPU, ≥1 GB RAM). Instalar
Docker Engine + plugin compose:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # re-loguea para que aplique
docker --version && docker compose version
```

---

## 1. Traer el código

```bash
git clone <URL_DEL_REPO> umbra-nocti
cd umbra-nocti
```

(Actualizaciones futuras: `git pull` y repetir el paso 4 con `--build`.)

---

## 2. Configurar secretos

```bash
cp .env.prod.example .env.prod
python3 -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))"
python3 -c "import secrets; print('ADMIN_TOKEN=' + secrets.token_urlsafe(32))"
# pega ambos valores en .env.prod
```

`.env.prod` está en `.gitignore` — no se commitea. `ADMIN_TOKEN` vacío deja los
endpoints `/admin/*` en fail-closed (nadie puede halt/flatten).

---

## 3. (Opcional) Probar el build sin levantar nada

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml build
```

---

## 4. Levantar el stack

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
```

Orden garantizado por healthchecks: Postgres y Redis sanos → la API arranca,
**aplica `alembic upgrade head` automáticamente** (entrypoint) y levanta uvicorn
→ el dashboard arranca cuando la API responde `/health`.

---

## 5. Verificar

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yml ps      # todos healthy
curl -fsS http://127.0.0.1:8000/health                                  # {"status":"ok"...}
docker compose --env-file .env.prod -f docker-compose.prod.yml logs -f api
```

Tras unos minutos deberías ver en los logs el universe scanner y el poller
generando snapshots. Las señales >3σ son raras: dale tiempo a acumular histórico.

---

## 6. Acceder al dashboard (sin exponerlo a internet)

La API y el dashboard se publican **solo en `127.0.0.1` del VPS** (el dashboard
no tiene autenticación). Para verlo desde tu máquina, túnel SSH:

```bash
# en TU máquina local
ssh -L 8501:localhost:8501 usuario@IP_DEL_VPS
# luego abre http://localhost:8501 en el navegador
```

Si quieres acceso público con login, pon un reverse proxy (Caddy/Nginx) con
basic-auth o un IdP delante del 8501 — no abras el puerto crudo en el firewall.

---

## 7. Operación

| Acción | Comando (prefijo `docker compose --env-file .env.prod -f docker-compose.prod.yml`) |
|---|---|
| Ver estado | `… ps` |
| Logs de un servicio | `… logs -f api` (o `dashboard`, `postgres`) |
| Reiniciar la API | `… restart api` |
| Parar todo (conserva datos) | `… down` |
| Parar y **borrar datos** | `… down -v`  ⚠️ destruye los volúmenes |
| Actualizar tras `git pull` | `… up -d --build` |
| Halt manual (kill-switch) | `curl -X POST 127.0.0.1:8000/admin/halt -H "X-Admin-Token: $ADMIN_TOKEN"` |

### Backup de Postgres

```bash
docker exec umbra_postgres pg_dump -U umbra umbra | gzip > umbra_$(date +%F).sql.gz
```

---

## 8. Notas

- **Python 3.11**: la imagen fija `python:3.11-slim` (el proyecto está pineado a
  `<3.12`); no dependes del Python del host.
- **Migraciones**: corren solas en cada arranque de la API (idempotentes).
- **Investigación de régimen** (Fase 0, offline): se corre puntualmente, no es un
  servicio. Dentro del contenedor:
  `docker exec umbra_api python scripts/run_regime_research.py --synthetic`
- **Recursos**: free tier de Postgres/Redis en contenedor basta para `sim`. Si
  subes el universo (`universe_top_n`) vigila RAM con `docker stats`.
