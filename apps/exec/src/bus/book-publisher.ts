/**
 * Publicador de books: WebSocket oficial de Polymarket → Redis.
 *
 * exec habla exactamente dos idiomas: Redis y Polymarket. No toca Postgres,
 * no tiene credenciales de la base de datos, y no llama a Gamma. El universo de
 * mercados y sus metadatos (liquidez, volumen) se los da brain por Redis.
 *
 * Lo único que aporta este proceso, y no es poco:
 *   - precio con ~1s de latencia en vez de los 30s del poller REST
 *   - **profundidad real del libro**, que Gamma no expone y sin la cual el
 *     modelo de slippage de brain es una heurística sobre `volume_24hr`
 *
 * No firma nada. No manda órdenes. Solo lee y publica.
 */

import type Redis from 'ioredis';
import { RealtimeServiceV2 } from '../services/realtime-service-v2.js';
import type { MarketSubscription, OrderbookSnapshot } from '../services/realtime-service-v2.js';
import {
  BOOK_TTL_SEC,
  UNIVERSE_KEY,
  bookKey,
  buildCachedBook,
  decodeUniverse,
  yesTokenId,
  type UniverseMarket,
} from './book.js';

export interface BookPublisherOptions {
  redis: Redis;
  realtime?: RealtimeServiceV2;
  /** Cada cuánto se relee el universo. brain lo reescribe cada 5 min. */
  universeRefreshMs?: number;
  logger?: Pick<Console, 'log' | 'warn' | 'error'>;
  now?: () => Date;
}

interface Stats {
  published: number;
  skippedCrossed: number;
  skippedEmpty: number;
  writeErrors: number;
  markets: number;
}

export class BookPublisher {
  private readonly redis: Redis;
  private readonly realtime: RealtimeServiceV2;
  private readonly universeRefreshMs: number;
  private readonly log: Pick<Console, 'log' | 'warn' | 'error'>;
  private readonly now: () => Date;

  /** token_id (YES) → mercado. Reconstruido en cada cambio de universo. */
  private byToken = new Map<string, UniverseMarket>();
  /** token_id → último precio de trade visto por el WS. */
  private lastTrade = new Map<string, number>();

  private subscription: MarketSubscription | null = null;
  private subscribedTokens: string[] = [];
  private timer: NodeJS.Timeout | null = null;
  private stopped = false;

  readonly stats: Stats = {
    published: 0,
    skippedCrossed: 0,
    skippedEmpty: 0,
    writeErrors: 0,
    markets: 0,
  };

  constructor(opts: BookPublisherOptions) {
    this.redis = opts.redis;
    this.realtime = opts.realtime ?? new RealtimeServiceV2({ autoReconnect: true });
    this.universeRefreshMs = opts.universeRefreshMs ?? 30_000;
    this.log = opts.logger ?? console;
    this.now = opts.now ?? (() => new Date());
  }

  async start(): Promise<void> {
    this.realtime.connect();
    await this.refreshUniverse();
    this.timer = setInterval(() => {
      void this.refreshUniverse().catch((err) =>
        this.log.warn('[book-publisher] refresco de universo fallido', err),
      );
    }, this.universeRefreshMs);
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.subscription?.unsubscribe();
    this.subscription = null;
    this.realtime.disconnect();
  }

  /**
   * Relee el universo y re-suscribe si cambió.
   *
   * Si el universo caducó (brain lleva >20 min sin escanear, o murió), nos
   * desuscribimos de todo y dejamos de publicar. Los books caducan solos a los
   * 60s y brain, cuando vuelva, no encontrará nada fresco y caerá a Gamma. El
   * sistema degrada solo; nadie tiene que darse cuenta.
   */
  async refreshUniverse(): Promise<void> {
    if (this.stopped) return;

    let raw: string | null;
    try {
      raw = await this.redis.get(UNIVERSE_KEY);
    } catch (err) {
      this.log.warn('[book-publisher] Redis no responde al leer el universo', err);
      return;
    }

    if (raw === null) {
      if (this.subscribedTokens.length) {
        this.log.warn('[book-publisher] universo caducado o ausente; dejo de publicar');
        this.resubscribe([], new Map());
      }
      return;
    }

    let markets: UniverseMarket[];
    try {
      markets = decodeUniverse(raw).markets;
    } catch (err) {
      this.log.error('[book-publisher] universo malformado; conservo la suscripción actual', err);
      return;
    }

    const byToken = new Map<string, UniverseMarket>();
    for (const m of markets) {
      const token = yesTokenId(m);
      if (token) byToken.set(token, m);
    }

    const tokens = [...byToken.keys()].sort();
    if (sameTokens(tokens, this.subscribedTokens)) return;

    this.log.log(
      `[book-publisher] universo: ${tokens.length} mercados (antes ${this.subscribedTokens.length})`,
    );
    this.resubscribe(tokens, byToken);
  }

  private resubscribe(tokens: string[], byToken: Map<string, UniverseMarket>): void {
    this.subscription?.unsubscribe();
    this.subscription = null;
    this.byToken = byToken;
    this.subscribedTokens = tokens;
    this.stats.markets = tokens.length;

    // Los precios de trade de tokens que ya no vigilamos solo ocupan memoria.
    for (const token of [...this.lastTrade.keys()]) {
      if (!byToken.has(token)) this.lastTrade.delete(token);
    }

    if (!tokens.length) return;

    this.subscription = this.realtime.subscribeMarkets(tokens, {
      onOrderbook: (book) => void this.onOrderbook(book),
      onLastTrade: (trade) => {
        this.lastTrade.set(trade.assetId, trade.price);
      },
      onError: (err) => this.log.warn('[book-publisher] error del WebSocket', err),
    });
  }

  /** Expuesto para los tests: la ruta caliente, sin el WebSocket de por medio. */
  async onOrderbook(book: OrderbookSnapshot): Promise<void> {
    const token = book.assetId ?? book.tokenId;
    const market = this.byToken.get(token);
    if (!market) return; // llegó tarde, tras un cambio de universo

    const result = buildCachedBook({
      market,
      book,
      lastTradePrice: this.lastTrade.get(token) ?? null,
      ts: this.now(),
    });

    if (!result.ok) {
      if (result.reason === 'crossed') this.stats.skippedCrossed++;
      else this.stats.skippedEmpty++;
      return;
    }

    try {
      await this.redis.set(
        bookKey(market.condition_id),
        JSON.stringify(result.book),
        'EX',
        BOOK_TTL_SEC,
      );
      this.stats.published++;
    } catch (err) {
      this.stats.writeErrors++;
      this.log.warn('[book-publisher] no pude escribir el book', market.condition_id, err);
    }
  }
}

function sameTokens(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((t, i) => t === b[i]);
}
