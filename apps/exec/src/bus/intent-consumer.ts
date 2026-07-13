/**
 * El consumidor de `nocti:intents`. La Fase 3.
 *
 * brain pide, exec responde. En `shadow` no se firma nada: se cotiza el intent
 * contra el libro real del token (`quote.ts`) y se publica en `nocti:fills` el
 * fill que **se habría** obtenido. brain lo guarda y lo resta contra lo que su
 * modelo de slippage había predicho. Esa resta es el entregable de la fase.
 *
 * EL CAMINO `live` NO ESTÁ ESCRITO, A PROPÓSITO
 * ---------------------------------------------
 * El plan decía «el camino live existe pero detrás de un flag apagado». Aquí hay
 * un seam (`LiveExecutor`) y no hay implementación. Escribir el código que firma
 * meses antes de ejercitarlo es la forma conocida de que la primera orden real la
 * mande un `if` mal leído. Un intent con `mode: live` se rechaza mientras nadie
 * inyecte un ejecutor; la Fase 4 lo inyecta, y ese día el código nace ya probado.
 *
 * LAS TRES REGLAS DEL CONTRATO (packages/contracts/README.md §35)
 * --------------------------------------------------------------
 * 1. **Idempotencia.** `SET nocti:intent:{id} … NX EX 86400` antes de tocar nada.
 *    Si la clave ya tenía un fill, se re-emite ese fill en vez de cotizar otro:
 *    un restart de brain reenvía los intents no ackeados, y en `live` eso serían
 *    dos órdenes con dinero real.
 * 2. **Halt fail-closed.** Se lee `umbra:halt` antes de cada intent. Si vale `"1"`,
 *    se rechaza. Si Redis no contesta, tampoco se ejecuta: la excepción sube, el
 *    mensaje se queda sin ackear y vuelve por `XAUTOCLAIM`. No ejecutar es seguro;
 *    ejecutar sin saber si estás haltado, no.
 * 3. **exec nunca dimensiona.** `size_usd` viene firmado por el risk engine de
 *    brain y solo se usa como techo. `quoteIntent` jamás gasta más.
 *
 * SOBRE EL `inflight` HUÉRFANO
 * ----------------------------
 * Si el proceso muere entre reclamar la clave y guardar el fill, la clave queda
 * en `inflight` 24 horas. En `shadow` se toma el relevo pasado `inflightTakeoverMs`
 * y se vuelve a cotizar: no hay nada que duplicar. En `live` **no se toma el
 * relevo nunca**: no sabemos si la orden llegó a firmarse, y averiguarlo es
 * reconciliar contra el CLOB, no adivinar. Se emite `ERROR`, se escribe
 * `umbra:halt`, y que lo mire una persona.
 */

import type { Redis } from 'ioredis';
import type { BookSource } from './book-source.js';
import {
  EXEC_GROUP,
  FILLS_STREAM,
  HALT_KEY,
  HALT_REASON_KEY,
  INTENTS_STREAM,
  INTENT_DEDUP_PREFIX,
  INTENT_DEDUP_TTL_SEC,
  type Fill,
  type Intent,
  emptyFill,
  encodeFill,
  fieldsFromEntry,
  parseIntent,
} from './intent.js';
import { quoteIntent } from './quote.js';

/** Donde acaban los mensajes que no son intents. No se ejecutan, no se pierden. */
export const DEAD_LETTER_STREAM = 'nocti:intents:dead';

/** Los streams no se podan solos. `~` deja que Redis pode cuando le venga bien. */
const FILLS_MAXLEN = 100_000;
const DEAD_LETTER_MAXLEN = 10_000;

/** La Fase 4 implementa esto. Hoy nadie lo inyecta y `live` se rechaza. */
export interface LiveExecutor {
  execute(intent: Intent): Promise<Fill>;
}

export interface IntentConsumerOptions {
  /** Cliente para comandos. */
  redis: Redis;
  /**
   * Cliente para el `XREADGROUP` bloqueante. Debe ser distinto del anterior: un
   * `BLOCK` deja la conexión muda para todo lo demás, incluido el `XACK` de la
   * respuesta que acabas de calcular.
   */
  reader: Redis;
  bookSource: BookSource;
  /** Identifica a este proceso dentro del grupo. Sale en `XPENDING`. */
  consumerName: string;
  executor?: LiveExecutor;
  feeBps?: number;
  blockMs?: number;
  batchSize?: number;
  /** Cuánto puede llevar un mensaje sin ackear antes de que otro lo reclame. */
  minIdleMs?: number;
  /** Cuánto vive un `inflight` antes de considerarse huérfano. */
  inflightTakeoverMs?: number;
  logger?: (...args: unknown[]) => void;
  now?: () => number;
}

export interface IntentConsumerStats {
  read: number;
  filled: number;
  partial: number;
  rejected: number;
  expired: number;
  errored: number;
  duplicates: number;
  reEmitted: number;
  invalid: number;
  reclaimed: number;
  bookErrors: number;
}

type DedupRecord =
  | { state: 'inflight'; consumer: string; ts: number }
  | { state: 'done'; fill: Fill };

type StreamEntry = [id: string, fields: string[]];

/** Qué hacer con el mensaje después de procesarlo. */
interface Outcome {
  /** `false` deja el mensaje en el PEL para que `XAUTOCLAIM` lo reintente. */
  ack: boolean;
}

const ACK: Outcome = { ack: true };
const RETRY: Outcome = { ack: false };

export class IntentConsumer {
  private readonly redis: Redis;
  private readonly reader: Redis;
  private readonly bookSource: BookSource;
  private readonly consumerName: string;
  private readonly executor: LiveExecutor | null;
  private readonly feeBps: number;
  private readonly blockMs: number;
  private readonly batchSize: number;
  private readonly minIdleMs: number;
  private readonly inflightTakeoverMs: number;
  private readonly log: (...args: unknown[]) => void;
  private readonly now: () => number;

  private stopped = false;
  private running: Promise<void> | null = null;

  readonly stats: IntentConsumerStats = {
    read: 0,
    filled: 0,
    partial: 0,
    rejected: 0,
    expired: 0,
    errored: 0,
    duplicates: 0,
    reEmitted: 0,
    invalid: 0,
    reclaimed: 0,
    bookErrors: 0,
  };

  constructor(options: IntentConsumerOptions) {
    this.redis = options.redis;
    this.reader = options.reader;
    this.bookSource = options.bookSource;
    this.consumerName = options.consumerName;
    this.executor = options.executor ?? null;
    this.feeBps = options.feeBps ?? 0;
    this.blockMs = options.blockMs ?? 5_000;
    this.batchSize = options.batchSize ?? 16;
    this.minIdleMs = options.minIdleMs ?? 60_000;
    this.inflightTakeoverMs = options.inflightTakeoverMs ?? 120_000;
    this.log = options.logger ?? (() => {});
    this.now = options.now ?? Date.now;
  }

  // -------------------------------------------------------------------------
  // Ciclo de vida
  // -------------------------------------------------------------------------

  /**
   * Crea el grupo desde `0` y no desde `$`.
   *
   * Con `$` se ignoraría todo lo que brain publicó antes de que exec arrancara
   * por primera vez, y eso es perder órdenes sin decirlo. Desde `0` se leen, y
   * las viejas mueren por `expires_at` con un `EXPIRED` que queda escrito.
   */
  async ensureGroup(): Promise<void> {
    try {
      await this.redis.xgroup('CREATE', INTENTS_STREAM, EXEC_GROUP, '0', 'MKSTREAM');
    } catch (err) {
      if (!String((err as Error).message).includes('BUSYGROUP')) throw err;
    }
  }

  async start(): Promise<void> {
    await this.ensureGroup();
    this.running = this.loop();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    // El `BLOCK` no se entera de un flag. Cortar la conexión sí lo despierta.
    this.reader.disconnect();
    await this.running?.catch(() => {});
    this.running = null;
  }

  private async loop(): Promise<void> {
    while (!this.stopped) {
      try {
        await this.reclaimPending();
        const entries = await this.readNew();
        for (const entry of entries) {
          if (this.stopped) break;
          await this.consume(entry);
        }
      } catch (err) {
        if (this.stopped) break;
        this.log('intents: fallo en el bucle:', (err as Error).message);
        await this.sleep(1_000);
      }
    }
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  private async readNew(): Promise<StreamEntry[]> {
    const res = (await this.reader.xreadgroup(
      'GROUP',
      EXEC_GROUP,
      this.consumerName,
      'COUNT',
      this.batchSize,
      'BLOCK',
      this.blockMs,
      'STREAMS',
      INTENTS_STREAM,
      '>',
    )) as [string, StreamEntry[]][] | null;

    if (!res?.length) return [];
    return res[0][1] ?? [];
  }

  /**
   * Recoge lo que otro consumidor leyó y nunca ackeó, porque murió.
   *
   * Sin esto, un intent leído por un proceso que se cae se queda en el PEL para
   * siempre: ni se ejecuta ni se rechaza, y brain espera un fill que no llega.
   */
  private async reclaimPending(): Promise<void> {
    const res = (await this.redis.xautoclaim(
      INTENTS_STREAM,
      EXEC_GROUP,
      this.consumerName,
      this.minIdleMs,
      '0',
      'COUNT',
      this.batchSize,
    )) as [string, StreamEntry[], ...unknown[]];

    const entries = (res?.[1] ?? []).filter((e): e is StreamEntry => Array.isArray(e?.[1]));
    for (const entry of entries) {
      if (this.stopped) break;
      this.stats.reclaimed += 1;
      await this.consume(entry);
    }
  }

  private async consume(entry: StreamEntry): Promise<void> {
    const [id, raw] = entry;
    this.stats.read += 1;
    const outcome = await this.handleEntry(fieldsFromEntry(raw));
    if (outcome.ack) {
      await this.redis.xack(INTENTS_STREAM, EXEC_GROUP, id);
    }
  }

  // -------------------------------------------------------------------------
  // El camino de un intent
  // -------------------------------------------------------------------------

  /** Expuesto para los tests: hace todo salvo leer del stream y ackear. */
  async handleEntry(fields: Record<string, string>): Promise<Outcome> {
    const parsed = parseIntent(fields);
    if (!parsed.ok) {
      // Un mensaje que no es un intent no se ejecuta ni se adivina. Se aparta,
      // se ackea, y queda en un stream aparte para que alguien lo mire.
      this.stats.invalid += 1;
      this.log('intents: descartado,', parsed.error);
      await this.redis.xadd(
        DEAD_LETTER_STREAM,
        'MAXLEN',
        '~',
        DEAD_LETTER_MAXLEN,
        '*',
        ...Object.entries(fields).flat(),
        '_error',
        parsed.error,
      );
      return ACK;
    }

    const intent = parsed.value;
    const nowMs = this.now();
    const ts = new Date(nowMs).toISOString();

    // Barato y sin I/O: primero.
    if (nowMs > Date.parse(intent.expires_at)) {
      this.stats.expired += 1;
      return this.emit(
        emptyFill(intent, 'EXPIRED', `llegó después de expires_at (${intent.expires_at})`, ts),
      );
    }

    // Fail-closed: si esta lectura revienta, la excepción sube y el mensaje se
    // queda sin ackear. Preferimos reintentarlo a ejecutarlo a ciegas.
    const halt = await this.readHalt();
    if (halt !== null) {
      this.stats.rejected += 1;
      return this.emit(emptyFill(intent, 'REJECTED', `halt activo: ${halt}`, ts));
    }

    if (intent.mode === 'live' && this.executor === null) {
      this.stats.rejected += 1;
      return this.emit(
        emptyFill(intent, 'REJECTED', 'camino live no implementado: no hay ejecutor (Fase 4)', ts),
      );
    }

    const claim = await this.claim(intent, ts);
    if (claim.kind === 'duplicate') return ACK;
    if (claim.kind === 'busy') return RETRY;
    if (claim.kind === 'orphan-live') {
      this.stats.errored += 1;
      return this.emit(claim.fill);
    }

    return this.execute(intent, ts);
  }

  private async execute(intent: Intent, ts: string): Promise<Outcome> {
    if (intent.mode === 'live') {
      // Inalcanzable hoy: `handleEntry` rechaza antes si no hay ejecutor.
      const fill = await this.executor!.execute(intent);
      return this.emit(fill, intent);
    }

    let book;
    try {
      book = await this.bookSource.fetch(intent.token_id);
    } catch (err) {
      // No hay libro, no hay cotización. Se suelta la reserva y se deja el
      // mensaje sin ackear: volverá por `XAUTOCLAIM`, y si el CLOB sigue caído
      // acabará muriendo de viejo con un `EXPIRED` honesto.
      this.stats.bookErrors += 1;
      this.log('intents: sin libro para', intent.token_id, (err as Error).message);
      await this.redis.del(dedupKey(intent.intent_id));
      return RETRY;
    }

    const quote = quoteIntent(
      {
        side: intent.side,
        sizeUsd: intent.size_usd,
        limitPrice: intent.limit_price,
        tif: intent.tif,
        maxSlippageBps: intent.max_slippage_bps,
        feeBps: this.feeBps,
      },
      book,
    );

    if (quote.status === 'FILLED') this.stats.filled += 1;
    else if (quote.status === 'PARTIAL') this.stats.partial += 1;
    else this.stats.rejected += 1;

    const fill: Fill = {
      intent_id: intent.intent_id,
      ts,
      mode: intent.mode,
      status: quote.status,
      filled_shares: quote.filledShares,
      avg_price: quote.avgPrice,
      notional_usd: quote.notionalUsd,
      fees_usd: quote.feesUsd,
      // En shadow no existe la orden. Que estos campos vayan vacíos es la marca
      // de que nada se firmó, y brain la usa para no confundir las dos cosas.
      order_id: '',
      tx_hash: '',
      // La referencia contra la que se midió el slippage. brain la guarda como
      // `mid_at_fill`: sin ella tendría que despejarla de la propia bps.
      mid_price: quote.midPrice,
      expected_slippage_bps: intent.expected_slippage_bps,
      realized_slippage_bps: quote.realizedSlippageBps,
      error: quote.error,
    };

    return this.emit(fill, intent);
  }

  // -------------------------------------------------------------------------
  // Idempotencia
  // -------------------------------------------------------------------------

  private async claim(
    intent: Intent,
    ts: string,
  ): Promise<
    | { kind: 'claimed' }
    | { kind: 'duplicate' }
    | { kind: 'busy' }
    | { kind: 'orphan-live'; fill: Fill }
  > {
    const key = dedupKey(intent.intent_id);
    const mine: DedupRecord = { state: 'inflight', consumer: this.consumerName, ts: this.now() };

    const acquired = await this.redis.set(
      key,
      JSON.stringify(mine),
      'EX',
      INTENT_DEDUP_TTL_SEC,
      'NX',
    );
    if (acquired) return { kind: 'claimed' };

    const raw = await this.redis.get(key);
    if (raw === null) {
      // Caducó entre el SET NX y el GET. Nadie lo tiene: se toma.
      await this.redis.set(key, JSON.stringify(mine), 'EX', INTENT_DEDUP_TTL_SEC);
      return { kind: 'claimed' };
    }

    const record = decodeDedup(raw);

    if (record?.state === 'done') {
      // Ya se procesó. Se re-emite el fill que salió entonces en vez de cotizar
      // otro: el libro de ahora no es el libro de entonces, y brain espera el
      // resultado de SU intent, no una segunda opinión.
      this.stats.duplicates += 1;
      this.stats.reEmitted += 1;
      this.log('intents: duplicado, re-emito el fill de', intent.intent_id);
      await this.publish(record.fill);
      return { kind: 'duplicate' };
    }

    const staleSince = record?.state === 'inflight' ? this.now() - record.ts : Number.POSITIVE_INFINITY;
    if (staleSince < this.inflightTakeoverMs) {
      // Otro consumidor lo tiene ahora mismo. Ni se ackea ni se toca.
      this.stats.duplicates += 1;
      return { kind: 'busy' };
    }

    // Huérfano: quien lo reclamó murió, o el valor está corrupto.
    if (intent.mode === 'live') {
      const reason = `intent ${intent.intent_id} huérfano en live: no se sabe si se firmó`;
      await this.haltEverything(reason);
      return {
        kind: 'orphan-live',
        fill: emptyFill(intent, 'ERROR', reason, ts),
      };
    }

    this.log('intents: relevo de un inflight huérfano', intent.intent_id);
    await this.redis.set(key, JSON.stringify(mine), 'EX', INTENT_DEDUP_TTL_SEC);
    return { kind: 'claimed' };
  }

  /** Guarda el fill bajo la clave de idempotencia y lo publica. */
  private async emit(fill: Fill, claimed?: Intent): Promise<Outcome> {
    if (claimed) {
      const record: DedupRecord = { state: 'done', fill };
      await this.redis.set(
        dedupKey(fill.intent_id),
        JSON.stringify(record),
        'EX',
        INTENT_DEDUP_TTL_SEC,
      );
    }
    await this.publish(fill);
    return ACK;
  }

  private async publish(fill: Fill): Promise<void> {
    await this.redis.xadd(FILLS_STREAM, 'MAXLEN', '~', FILLS_MAXLEN, '*', ...encodeFill(fill));
  }

  // -------------------------------------------------------------------------
  // Halt
  // -------------------------------------------------------------------------

  /** `null` si no hay halt; si lo hay, el motivo (o `"1"` si nadie lo escribió). */
  private async readHalt(): Promise<string | null> {
    const flag = await this.redis.get(HALT_KEY);
    if (flag !== '1') return null;
    const reason = await this.redis.get(HALT_REASON_KEY);
    return reason && reason.length > 0 ? reason : '1';
  }

  private async haltEverything(reason: string): Promise<void> {
    this.log('intents: HALT —', reason);
    await this.redis.set(HALT_KEY, '1');
    await this.redis.set(HALT_REASON_KEY, reason);
  }
}

export function dedupKey(intentId: string): string {
  return `${INTENT_DEDUP_PREFIX}${intentId}`;
}

function decodeDedup(raw: string): DedupRecord | null {
  try {
    const parsed = JSON.parse(raw) as DedupRecord;
    if (parsed?.state === 'done' || parsed?.state === 'inflight') return parsed;
    return null;
  } catch {
    // Corrupto. Se trata como un huérfano: en shadow se recotiza, en live se halta.
    return null;
  }
}
