/**
 * Nocti exec — el brazo.
 *
 * Fase 1: publica el book de Polymarket en Redis para que brain lo lea.
 * No firma nada, no manda órdenes, no toca Postgres.
 *
 * El bot antiguo (`bot-with-dashboard.ts`) sigue existiendo y se ejecuta aparte.
 * Este proceso no lo sustituye todavía; eso es la Fase 2.
 *
 *   npx tsx nocti-exec.ts
 */

import { createServer } from 'node:http';
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import dotenv from 'dotenv';
import Redis from 'ioredis';
import { BookPublisher } from './src/bus/book-publisher.js';

const here = dirname(fileURLToPath(import.meta.url));

// El .env vive en la raíz del monorepo: uno solo para brain y exec. Si alguien
// corre esto suelto, respetamos un .env local.
for (const candidate of [resolve(here, '../../.env'), resolve(here, '.env')]) {
  if (existsSync(candidate)) {
    dotenv.config({ path: candidate });
    break;
  }
}

const REDIS_URL = process.env.REDIS_URL ?? 'redis://localhost:6379/0';
const PORT = Number(process.env.NOCTI_EXEC_PORT ?? 3001);
const PUBLISHER_ENABLED = process.env.NOCTI_BOOK_PUBLISHER_ENABLED === 'true';
const EXEC_MODE = process.env.NOCTI_EXEC_MODE ?? 'off';

function log(...args: unknown[]): void {
  console.log(new Date().toISOString(), '[nocti-exec]', ...args);
}

async function main(): Promise<void> {
  log(`arrancando. publisher=${PUBLISHER_ENABLED ? 'on' : 'off'} exec_mode=${EXEC_MODE}`);

  if (EXEC_MODE !== 'off') {
    // El consumidor de intents es la Fase 3. Que exista la variable no significa
    // que el camino esté escrito: fallar aquí es mejor que fingir que se ejecuta.
    log(`NOCTI_EXEC_MODE=${EXEC_MODE} pero el consumidor de intents aún no existe (Fase 3).`);
    process.exit(1);
  }

  const redis = new Redis(REDIS_URL, {
    // Sin límite de reintentos: si Redis cae, exec espera. No publicar es seguro;
    // morir y que compose lo reinicie en bucle, no tanto.
    maxRetriesPerRequest: null,
    lazyConnect: false,
  });
  redis.on('error', (err) => log('redis:', err.message));

  let publisher: BookPublisher | null = null;
  if (PUBLISHER_ENABLED) {
    publisher = new BookPublisher({ redis, logger: console });
    await publisher.start();
    log('publicador de books activo');
  } else {
    log('publicador desactivado (NOCTI_BOOK_PUBLISHER_ENABLED != true). brain seguirá con su poller REST.');
  }

  const server = createServer((req, res) => {
    if (req.url !== '/health') {
      res.writeHead(404).end();
      return;
    }
    const healthy = redis.status === 'ready';
    res.writeHead(healthy ? 200 : 503, { 'content-type': 'application/json' });
    res.end(
      JSON.stringify({
        status: healthy ? 'ok' : 'degraded',
        redis: redis.status,
        publisher: PUBLISHER_ENABLED ? (publisher?.stats ?? null) : 'disabled',
      }),
    );
  });
  server.listen(PORT, () => log(`health en http://127.0.0.1:${PORT}/health`));

  let closing = false;
  const shutdown = async (signal: string): Promise<void> => {
    if (closing) return;
    closing = true;
    log(`${signal}: cerrando`);
    server.close();
    await publisher?.stop();
    await redis.quit().catch(() => redis.disconnect());
    process.exit(0);
  };
  process.on('SIGINT', () => void shutdown('SIGINT'));
  process.on('SIGTERM', () => void shutdown('SIGTERM'));
}

main().catch((err) => {
  console.error('[nocti-exec] fallo al arrancar:', err);
  process.exit(1);
});
