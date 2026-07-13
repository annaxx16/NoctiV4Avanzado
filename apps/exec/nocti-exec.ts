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
import { hostname } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import dotenv from 'dotenv';
import Redis from 'ioredis';
import { BookPublisher } from './src/bus/book-publisher.js';
import { ClobBookSource } from './src/bus/book-source.js';
import { IntentConsumer } from './src/bus/intent-consumer.js';

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
/** `off` | `shadow`. `live` no arranca: no hay ejecutor que firme (Fase 4). */
const EXEC_MODE = process.env.NOCTI_EXEC_MODE ?? 'off';
const FEE_BPS = Number(process.env.NOCTI_FEE_BPS ?? 0);

function log(...args: unknown[]): void {
  console.log(new Date().toISOString(), '[nocti-exec]', ...args);
}

async function main(): Promise<void> {
  log(`arrancando. publisher=${PUBLISHER_ENABLED ? 'on' : 'off'} exec_mode=${EXEC_MODE}`);

  if (EXEC_MODE !== 'off' && EXEC_MODE !== 'shadow') {
    // `live` incluido. El seam `LiveExecutor` existe en `intent-consumer.ts` y
    // nadie lo implementa: arrancar en live solo produciría rechazos. Fallar aquí
    // es más claro que arrancar y rechazarlo todo.
    log(`NOCTI_EXEC_MODE=${EXEC_MODE} no está soportado. Válidos: off | shadow.`);
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

  let consumer: IntentConsumer | null = null;
  let reader: Redis | null = null;
  if (EXEC_MODE === 'shadow') {
    // Una conexión aparte para el `XREADGROUP` bloqueante: un `BLOCK` deja la
    // conexión muda para todo lo demás, incluido el `XACK` de lo que acaba de leer.
    reader = redis.duplicate();
    reader.on('error', (err) => log('redis(reader):', err.message));

    consumer = new IntentConsumer({
      redis,
      reader,
      // Sin wallet. En shadow no se firma, así que exec no necesita PRIVATE_KEY.
      bookSource: new ClobBookSource(),
      consumerName: `${hostname()}-${process.pid}`,
      feeBps: FEE_BPS,
      logger: log,
    });
    await consumer.start();
    log('consumidor de intents activo en shadow: cotiza contra el libro real, no firma');
  } else {
    log('consumidor de intents desactivado (NOCTI_EXEC_MODE != shadow)');
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
        intents: consumer?.stats ?? 'disabled',
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
    await consumer?.stop();
    await reader?.quit().catch(() => reader?.disconnect());
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
