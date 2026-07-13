import { describe, expect, it, vi } from 'vitest';
import type { Redis } from 'ioredis';
import { IntentConsumer, DEAD_LETTER_STREAM, dedupKey, type LiveExecutor } from './intent-consumer.js';
import { FILLS_STREAM, HALT_KEY, HALT_REASON_KEY } from './intent.js';
import type { BookSource } from './book-source.js';
import type { QuoteBook } from './quote.js';

const T0 = Date.parse('2026-07-10T12:00:00.000Z');
const INTENT_ID = '3f2504e0-4f89-41d3-9a0c-0305e82c3301';
const CID = '0x' + 'ab'.repeat(32);

function intentFields(over: Record<string, string> = {}): Record<string, string> {
  return {
    intent_id: INTENT_ID,
    ts: new Date(T0).toISOString(),
    strategy: 'overreaction',
    mode: 'shadow',
    condition_id: CID,
    token_id: 'tok_yes',
    side: 'BUY',
    size_usd: '62',
    limit_price: '0.99',
    tif: 'IOC',
    max_slippage_bps: '1000',
    expires_at: new Date(T0 + 60_000).toISOString(),
    expected_slippage_bps: '30',
    ...over,
  };
}

const BOOK: QuoteBook = {
  bids: [{ price: '0.61', size: '1200' }],
  asks: [{ price: '0.62', size: '100' }],
};

interface Fake {
  redis: Redis;
  store: Map<string, string>;
  xadds: Array<{ stream: string; args: string[] }>;
  acked: string[];
  failGetOnce(): void;
}

function fakeRedis(): Fake {
  const store = new Map<string, string>();
  const xadds: Array<{ stream: string; args: string[] }> = [];
  const acked: string[] = [];
  let failGet = 0;

  const redis = {
    get: vi.fn(async (key: string) => {
      if (failGet > 0) {
        failGet -= 1;
        throw new Error('redis caído');
      }
      return store.get(key) ?? null;
    }),
    set: vi.fn(async (key: string, value: string, ...rest: unknown[]) => {
      if (rest.includes('NX') && store.has(key)) return null;
      store.set(key, value);
      return 'OK';
    }),
    del: vi.fn(async (key: string) => (store.delete(key) ? 1 : 0)),
    xadd: vi.fn(async (stream: string, ...args: unknown[]) => {
      xadds.push({ stream, args: args.map(String) });
      return '1-1';
    }),
    xack: vi.fn(async (_s: string, _g: string, id: string) => {
      acked.push(id);
      return 1;
    }),
  } as unknown as Redis;

  return {
    redis,
    store,
    xadds,
    acked,
    failGetOnce: () => {
      failGet += 1;
    },
  };
}

function fakeBookSource(book: QuoteBook | Error = BOOK): BookSource & { calls: string[] } {
  const calls: string[] = [];
  return {
    calls,
    async fetch(tokenId: string) {
      calls.push(tokenId);
      if (book instanceof Error) throw book;
      return book;
    },
  };
}

function build(over: Partial<ConstructorParameters<typeof IntentConsumer>[0]> = {}) {
  const fake = fakeRedis();
  const bookSource = fakeBookSource();
  const consumer = new IntentConsumer({
    redis: fake.redis,
    reader: fake.redis,
    bookSource,
    consumerName: 'exec-1',
    now: () => T0,
    ...over,
  });
  return { consumer, fake, bookSource };
}

/** Los campos planos del último XADD a un stream, como objeto. */
function lastFill(fake: Fake): Record<string, string> {
  const entry = [...fake.xadds].reverse().find((x) => x.stream === FILLS_STREAM);
  if (!entry) throw new Error('no se publicó ningún fill');
  // args = ['MAXLEN', '~', N, '*', k, v, k, v, ...]
  const flat = entry.args.slice(4);
  const out: Record<string, string> = {};
  for (let i = 0; i + 1 < flat.length; i += 2) out[flat[i]] = flat[i + 1];
  return out;
}

describe('IntentConsumer — el camino shadow', () => {
  it('cotiza contra el libro real y publica el fill que se habría obtenido', async () => {
    const { consumer, fake, bookSource } = build();

    const outcome = await consumer.handleEntry(intentFields());

    expect(outcome.ack).toBe(true);
    expect(bookSource.calls).toEqual(['tok_yes']);

    const fill = lastFill(fake);
    expect(fill.intent_id).toBe(INTENT_ID);
    expect(fill.mode).toBe('shadow');
    expect(fill.status).toBe('FILLED');
    expect(fill.filled_shares).toBe('100.000000');
    expect(fill.notional_usd).toBe('62.000000');
    expect(fill.avg_price).toBe('0.620000');
    // Nada se firmó, y estos dos campos vacíos son la prueba.
    expect(fill.order_id).toBe('');
    expect(fill.tx_hash).toBe('');
  });

  it('copia expected_slippage_bps y le pone al lado el realizado', async () => {
    const { consumer, fake } = build();
    await consumer.handleEntry(intentFields());

    const fill = lastFill(fake);
    // Lo que brain predijo con volume_24hr como proxy…
    expect(fill.expected_slippage_bps).toBe('30');
    // …y lo que el libro real habría cobrado. La resta es la Fase 3.
    expect(fill.realized_slippage_bps).toBe('81');
    // Y el mid contra el que se midió, para que brain no tenga que despejarlo.
    expect(fill.mid_price).toBe('0.615000');
  });

  it('deja el fill guardado bajo la clave de idempotencia', async () => {
    const { consumer, fake } = build();
    await consumer.handleEntry(intentFields());

    const record = JSON.parse(fake.store.get(dedupKey(INTENT_ID))!);
    expect(record.state).toBe('done');
    expect(record.fill.status).toBe('FILLED');
  });

  it('un intent rechazado por slippage también se guarda: el rechazo es un dato', async () => {
    const { consumer, fake } = build();
    await consumer.handleEntry(intentFields({ max_slippage_bps: '50' }));

    const fill = lastFill(fake);
    expect(fill.status).toBe('REJECTED');
    expect(fill.realized_slippage_bps).toBe('81');
    expect(fill.error).toContain('slippage 81bps > max_slippage_bps 50');
  });
});

describe('IntentConsumer — idempotencia', () => {
  it('re-emite el fill anterior en vez de cotizar otro', async () => {
    const { consumer, fake, bookSource } = build();

    await consumer.handleEntry(intentFields());
    const first = lastFill(fake);
    expect(bookSource.calls).toHaveLength(1);

    const outcome = await consumer.handleEntry(intentFields());

    expect(outcome.ack).toBe(true);
    // No se volvió a mirar el libro: el de ahora no es el de entonces.
    expect(bookSource.calls).toHaveLength(1);
    expect(lastFill(fake)).toEqual(first);
    expect(consumer.stats.reEmitted).toBe(1);
  });

  it('no toca un intent que otro consumidor tiene en vuelo', async () => {
    const { consumer, fake, bookSource } = build();
    fake.store.set(
      dedupKey(INTENT_ID),
      JSON.stringify({ state: 'inflight', consumer: 'exec-2', ts: T0 - 1_000 }),
    );

    const outcome = await consumer.handleEntry(intentFields());

    // Sin ack: vuelve por XAUTOCLAIM cuando el otro lleve rato callado.
    expect(outcome.ack).toBe(false);
    expect(bookSource.calls).toHaveLength(0);
    expect(fake.xadds).toHaveLength(0);
  });

  it('en shadow toma el relevo de un inflight huérfano y recotiza', async () => {
    const { consumer, fake, bookSource } = build({ inflightTakeoverMs: 10_000 });
    fake.store.set(
      dedupKey(INTENT_ID),
      JSON.stringify({ state: 'inflight', consumer: 'muerto', ts: T0 - 60_000 }),
    );

    const outcome = await consumer.handleEntry(intentFields());

    expect(outcome.ack).toBe(true);
    expect(bookSource.calls).toEqual(['tok_yes']);
    expect(lastFill(fake).status).toBe('FILLED');
  });

  it('un valor corrupto se trata como huérfano, no como un fill', async () => {
    const { consumer, fake, bookSource } = build();
    fake.store.set(dedupKey(INTENT_ID), 'no soy json');

    await consumer.handleEntry(intentFields());
    expect(bookSource.calls).toEqual(['tok_yes']);
  });

  it('en live NO toma el relevo: halta y pide una persona', async () => {
    const executor: LiveExecutor = { execute: vi.fn() };
    const { consumer, fake } = build({ executor, inflightTakeoverMs: 10_000 });
    fake.store.set(
      dedupKey(INTENT_ID),
      JSON.stringify({ state: 'inflight', consumer: 'muerto', ts: T0 - 60_000 }),
    );

    await consumer.handleEntry(intentFields({ mode: 'live' }));

    expect(executor.execute).not.toHaveBeenCalled();
    expect(fake.store.get(HALT_KEY)).toBe('1');
    expect(fake.store.get(HALT_REASON_KEY)).toContain('no se sabe si se firmó');

    const fill = lastFill(fake);
    expect(fill.status).toBe('ERROR');
    expect(fill.filled_shares).toBe('0.000000');
  });
});

describe('IntentConsumer — las compuertas', () => {
  it('un intent que llega tarde expira, y ni mira el libro', async () => {
    const { consumer, fake, bookSource } = build({ now: () => T0 + 120_000 });

    await consumer.handleEntry(intentFields());

    expect(bookSource.calls).toHaveLength(0);
    const fill = lastFill(fake);
    expect(fill.status).toBe('EXPIRED');
    expect(fill.error).toContain('expires_at');
  });

  it('con umbra:halt puesto, rechaza y dice por qué', async () => {
    const { consumer, fake, bookSource } = build();
    fake.store.set(HALT_KEY, '1');
    fake.store.set(HALT_REASON_KEY, 'drawdown del 40%');

    await consumer.handleEntry(intentFields());

    expect(bookSource.calls).toHaveLength(0);
    const fill = lastFill(fake);
    expect(fill.status).toBe('REJECTED');
    expect(fill.error).toBe('halt activo: drawdown del 40%');
  });

  it('fail-closed: si Redis no dice si hay halt, no se ejecuta nada', async () => {
    const { consumer, fake, bookSource } = build();
    fake.failGetOnce();

    await expect(consumer.handleEntry(intentFields())).rejects.toThrow('redis caído');

    // Ni cotizó, ni publicó, ni ackeó. El mensaje sigue en el PEL.
    expect(bookSource.calls).toHaveLength(0);
    expect(fake.xadds).toHaveLength(0);
  });

  it('mode live sin ejecutor se rechaza: el camino no está escrito', async () => {
    const { consumer, fake } = build();

    await consumer.handleEntry(intentFields({ mode: 'live' }));

    const fill = lastFill(fake);
    expect(fill.status).toBe('REJECTED');
    expect(fill.error).toContain('camino live no implementado');
  });

  it('exec nunca gasta más de lo que brain firmó', async () => {
    const { consumer, fake } = build();
    await consumer.handleEntry(intentFields({ size_usd: '50' }));
    expect(Number(lastFill(fake).notional_usd)).toBeLessThanOrEqual(50);
  });
});

describe('IntentConsumer — lo que no es un intent', () => {
  it('un mensaje inválido se aparta a la dead letter y se ackea', async () => {
    const { consumer, fake, bookSource } = build();

    const outcome = await consumer.handleEntry({ intent_id: 'no-soy-un-uuid', mode: 'shadow' });

    expect(outcome.ack).toBe(true);
    expect(bookSource.calls).toHaveLength(0);
    expect(consumer.stats.invalid).toBe(1);

    const dead = fake.xadds.find((x) => x.stream === DEAD_LETTER_STREAM);
    expect(dead).toBeDefined();
    expect(dead!.args.join(' ')).toContain('falta el campo requerido');
    // No se publicó fill: no sabemos siquiera de qué intent hablaba.
    expect(fake.xadds.some((x) => x.stream === FILLS_STREAM)).toBe(false);
  });

  it('si el CLOB no da libro, suelta la reserva y deja el mensaje para reintentar', async () => {
    const bookSource = fakeBookSource(new Error('502 Bad Gateway'));
    const { consumer, fake } = build({ bookSource });

    const outcome = await consumer.handleEntry(intentFields());

    expect(outcome.ack).toBe(false);
    expect(fake.store.has(dedupKey(INTENT_ID))).toBe(false);
    expect(fake.xadds).toHaveLength(0);
    expect(consumer.stats.bookErrors).toBe(1);
  });
});
