import { beforeEach, describe, expect, it, vi } from 'vitest';
import type Redis from 'ioredis';
import type { OrderbookSnapshot } from '../services/realtime-service-v2.js';
import type { MarketFeed } from '../services/clob-market-socket.js';
import { BookPublisher } from './book-publisher.js';
import { BOOK_TTL_SEC, UNIVERSE_KEY, bookKey, type UniverseMarket } from './book.js';

const CID_A = '0x' + 'aa'.repeat(32);
const CID_B = '0x' + 'bb'.repeat(32);
const TS = new Date('2026-07-08T12:00:00.000Z');

const marketA: UniverseMarket = {
  condition_id: CID_A,
  rank: 1,
  token_ids: ['tok_a_yes', 'tok_a_no'],
  yes_token_id: 'tok_a_yes',
  liquidity_num: 1000,
  volume_24hr: 2000,
};
const marketB: UniverseMarket = {
  condition_id: CID_B,
  rank: 2,
  token_ids: ['tok_b_yes', 'tok_b_no'],
  yes_token_id: 'tok_b_yes',
  liquidity_num: 3000,
  volume_24hr: 4000,
};

/** Redis de mentira, con las tres operaciones que el publicador usa. */
function fakeRedis() {
  const store = new Map<string, string>();
  const sets: Array<{ key: string; value: string; ttl: number }> = [];
  let failNextSet = false;
  return {
    store,
    sets,
    failSetOnce: () => {
      failNextSet = true;
    },
    get: vi.fn(async (key: string) => store.get(key) ?? null),
    set: vi.fn(async (key: string, value: string, _ex: string, ttl: number) => {
      if (failNextSet) {
        failNextSet = false;
        throw new Error('redis caído');
      }
      store.set(key, value);
      sets.push({ key, value, ttl });
      return 'OK';
    }),
  };
}

function fakeRealtime() {
  const state = {
    connected: false,
    subscriptions: 0,
    unsubscribes: 0,
    tokens: [] as string[],
    handlers: null as null | {
      onOrderbook?: (b: OrderbookSnapshot) => void;
      onLastTrade?: (t: { assetId: string; price: number }) => void;
    },
  };
  const rt = {
    state,
    connect: vi.fn(() => {
      state.connected = true;
      return rt;
    }),
    disconnect: vi.fn(() => {
      state.connected = false;
    }),
    subscribeMarkets: vi.fn((tokens: string[], handlers: typeof state.handlers) => {
      state.subscriptions++;
      state.tokens = tokens;
      state.handlers = handlers;
      return {
        id: `s${state.subscriptions}`,
        topic: 'clob_market',
        type: '*',
        tokenIds: tokens,
        unsubscribe: () => {
          state.unsubscribes++;
          state.handlers = null;
          state.tokens = [];
        },
      };
    }),
  };
  return rt;
}

function ob(assetId: string, over: Partial<OrderbookSnapshot> = {}): OrderbookSnapshot {
  return {
    tokenId: assetId,
    assetId,
    market: 'ignorado',
    tickSize: '0.01',
    minOrderSize: '5',
    hash: 'h',
    timestamp: TS.getTime(),
    bids: [{ price: 0.61, size: 100 }],
    asks: [{ price: 0.62, size: 100 }],
    ...over,
  } as OrderbookSnapshot;
}

const silent = { log: () => {}, warn: () => {}, error: () => {} };

function makePublisher() {
  const redis = fakeRedis();
  const realtime = fakeRealtime();
  const pub = new BookPublisher({
    redis: redis as unknown as Redis,
    realtime: realtime as unknown as MarketFeed,
    logger: silent,
    now: () => TS,
  });
  return { pub, redis, realtime };
}

function publishUniverse(redis: ReturnType<typeof fakeRedis>, markets: UniverseMarket[]) {
  redis.store.set(UNIVERSE_KEY, JSON.stringify({ ts: TS.toISOString(), markets }));
}

describe('BookPublisher — universo', () => {
  let ctx: ReturnType<typeof makePublisher>;
  beforeEach(() => {
    ctx = makePublisher();
  });

  it('se suscribe solo a los tokens de YES', async () => {
    publishUniverse(ctx.redis, [marketA, marketB]);
    await ctx.pub.refreshUniverse();
    expect(ctx.realtime.state.tokens).toEqual(['tok_a_yes', 'tok_b_yes']);
  });

  it('salta los mercados sin YES identificable en vez de adivinar', async () => {
    publishUniverse(ctx.redis, [{ ...marketA, yes_token_id: null }, marketB]);
    await ctx.pub.refreshUniverse();
    expect(ctx.realtime.state.tokens).toEqual(['tok_b_yes']);
  });

  it('no re-suscribe si el universo no cambió', async () => {
    publishUniverse(ctx.redis, [marketA]);
    await ctx.pub.refreshUniverse();
    await ctx.pub.refreshUniverse();
    expect(ctx.realtime.state.subscriptions).toBe(1);
  });

  it('re-suscribe cuando el universo cambia', async () => {
    publishUniverse(ctx.redis, [marketA]);
    await ctx.pub.refreshUniverse();
    publishUniverse(ctx.redis, [marketA, marketB]);
    await ctx.pub.refreshUniverse();
    expect(ctx.realtime.state.subscriptions).toBe(2);
    expect(ctx.realtime.state.unsubscribes).toBe(1);
  });

  it('si el universo caduca, deja de publicar', async () => {
    // brain murió. Sus books caducan a los 60s y, cuando vuelva, su poller no
    // encontrará nada fresco y caerá a Gamma. Degradar es barato.
    publishUniverse(ctx.redis, [marketA]);
    await ctx.pub.refreshUniverse();
    ctx.redis.store.delete(UNIVERSE_KEY);
    await ctx.pub.refreshUniverse();
    expect(ctx.realtime.state.unsubscribes).toBe(1);
    expect(ctx.realtime.state.tokens).toEqual([]);
  });

  it('un universo malformado conserva la suscripción en vez de tirarla', async () => {
    publishUniverse(ctx.redis, [marketA]);
    await ctx.pub.refreshUniverse();
    ctx.redis.store.set(UNIVERSE_KEY, '{"ts":"x"}');
    await ctx.pub.refreshUniverse();
    expect(ctx.realtime.state.unsubscribes).toBe(0);
    expect(ctx.realtime.state.tokens).toEqual(['tok_a_yes']);
  });

  it('si Redis no responde, conserva la suscripción actual', async () => {
    publishUniverse(ctx.redis, [marketA]);
    await ctx.pub.refreshUniverse();
    ctx.redis.get.mockRejectedValueOnce(new Error('sin conexión'));
    await ctx.pub.refreshUniverse();
    expect(ctx.realtime.state.tokens).toEqual(['tok_a_yes']);
  });
});

describe('BookPublisher — publicación', () => {
  let ctx: ReturnType<typeof makePublisher>;
  beforeEach(async () => {
    ctx = makePublisher();
    publishUniverse(ctx.redis, [marketA]);
    await ctx.pub.refreshUniverse();
  });

  it('escribe en la clave que brain lee, con TTL', async () => {
    await ctx.pub.onOrderbook(ob('tok_a_yes'));
    const write = ctx.redis.sets.at(-1)!;
    expect(write.key).toBe(bookKey(CID_A));
    expect(write.ttl).toBe(BOOK_TTL_SEC);
    expect(JSON.parse(write.value)).toMatchObject({
      condition_id: CID_A,
      best_bid: 0.61,
      best_ask: 0.62,
      source: 'clob_ws',
      liquidity_num: 1000,
    });
    expect(ctx.pub.stats.published).toBe(1);
  });

  it('el último trade visto acompaña al siguiente book', async () => {
    ctx.realtime.state.handlers!.onLastTrade!({ assetId: 'tok_a_yes', price: 0.615 });
    await ctx.pub.onOrderbook(ob('tok_a_yes'));
    expect(JSON.parse(ctx.redis.sets.at(-1)!.value).last_trade_price).toBe(0.615);
  });

  it('no publica un libro cruzado', async () => {
    await ctx.pub.onOrderbook(
      ob('tok_a_yes', { bids: [{ price: 0.7, size: 1 }], asks: [{ price: 0.65, size: 1 }] }),
    );
    expect(ctx.redis.sets).toHaveLength(0);
    expect(ctx.pub.stats.skippedCrossed).toBe(1);
  });

  it('ignora books de tokens que ya no vigilamos', async () => {
    // Llegan tarde, justo después de un cambio de universo.
    await ctx.pub.onOrderbook(ob('tok_b_yes'));
    expect(ctx.redis.sets).toHaveLength(0);
  });

  it('ignora el token de NO aunque llegue por el mismo canal', async () => {
    await ctx.pub.onOrderbook(ob('tok_a_no'));
    expect(ctx.redis.sets).toHaveLength(0);
  });

  it('un fallo de escritura se cuenta y no tumba el proceso', async () => {
    ctx.redis.failSetOnce();
    await expect(ctx.pub.onOrderbook(ob('tok_a_yes'))).resolves.toBeUndefined();
    expect(ctx.pub.stats.writeErrors).toBe(1);
    expect(ctx.pub.stats.published).toBe(0);
  });

  it('olvida el precio de trade de un token que sale del universo', async () => {
    ctx.realtime.state.handlers!.onLastTrade!({ assetId: 'tok_a_yes', price: 0.615 });
    publishUniverse(ctx.redis, [marketB]);
    await ctx.pub.refreshUniverse();
    publishUniverse(ctx.redis, [marketA]);
    await ctx.pub.refreshUniverse();

    await ctx.pub.onOrderbook(ob('tok_a_yes'));
    expect(JSON.parse(ctx.redis.sets.at(-1)!.value).last_trade_price).toBeNull();
  });
});
