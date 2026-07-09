# Nocti exec — el brazo. CLOB, firma, CTF, WebSocket.
# Node 20; el proyecto corre con tsx (no compila a dist en runtime).
FROM node:20-slim AS dashboard-build
WORKDIR /build
COPY dashboard/package.json dashboard/package-lock.json* ./dashboard/
RUN cd dashboard && npm ci --no-audit --no-fund
COPY dashboard ./dashboard
RUN cd dashboard && npm run build


FROM node:20-slim AS runtime

ENV NODE_ENV=production \
    NPM_CONFIG_UPDATE_NOTIFIER=false

WORKDIR /app

# curl para los healthchecks de compose.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Capa de dependencias (cacheable). tsx es devDependency pero lo necesitamos en
# runtime: el bot se ejecuta sin compilar. Por eso `npm ci` completo, no --omit=dev.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# Código.
COPY tsconfig.json ./
COPY src ./src
COPY bot-config.ts bot-with-dashboard.ts ./
COPY scripts ./scripts

# El dashboard React ya viene compilado de la etapa anterior.
COPY --from=dashboard-build /build/dashboard/dist ./dashboard/dist

# Usuario no-root, mismo uid que brain para que compartan volúmenes sin sorpresas.
RUN useradd --create-home --uid 10001 nocti && chown -R nocti:nocti /app
USER nocti

EXPOSE 3001

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fsS http://127.0.0.1:3001/health || exit 1

CMD ["npx", "tsx", "bot-with-dashboard.ts"]
