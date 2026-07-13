/**
 * Cliente del canal `market` del WebSocket del CLOB de Polymarket.
 *
 * Por qué existe, si ya estaba `RealtimeServiceV2`:
 *
 *   Polymarket sacó los datos de CLOB del socket de datos en tiempo real (RTDS,
 *   `ws-live-data.polymarket.com`, que es lo que usa `@polymarket/real-time-data-client`).
 *   Ese socket ahora responde `"CLOB messages are not supported anymore"` (400) a
 *   cualquier suscripción `clob_market`. La profundidad de libro vive en el socket
 *   dedicado del CLOB:
 *
 *       wss://ws-subscriptions-clob.polymarket.com/ws/market
 *
 * Este cliente habla ese canal y nada más. No firma, no autentica: el canal
 * `market` es público. Expone la interfaz mínima que `BookPublisher` consume
 * (`MarketFeed`), así que el publicador no sabe —ni le importa— por dónde llega
 * el libro.
 *
 * El canal manda un `book` (snapshot completo) al suscribirse y luego `price_change`
 * (deltas por nivel). Para publicar un libro fresco hay que mantener el estado:
 * el snapshot siembra, cada delta lo actualiza. Publicar solo el primer snapshot
 * dejaría el libro congelado en el instante de la suscripción.
 */

import WebSocket from 'ws';
import type { OrderbookSnapshot } from './realtime-service-v2.js';

export const CLOB_MARKET_WS_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/market';

/** Cada cuánto mandar PING. El server corta a los ~10s sin heartbeat. */
const PING_INTERVAL_MS = 10_000;
/** Espera antes de reconectar tras una caída inesperada. */
const RECONNECT_DELAY_MS = 2_000;

export interface MarketFeedHandlers {
  onOrderbook?: (book: OrderbookSnapshot) => void;
  onLastTrade?: (trade: { assetId: string; price: number }) => void;
  onError?: (error: Error) => void;
}

export interface MarketSubscriptionHandle {
  unsubscribe: () => void;
}

/**
 * Lo único que `BookPublisher` necesita de un feed de mercado. `RealtimeServiceV2`
 * lo cumplía; `ClobMarketSocket` lo cumple ahora contra el endpoint correcto.
 */
export interface MarketFeed {
  connect(): void;
  disconnect(): void;
  subscribeMarkets(tokenIds: string[], handlers: MarketFeedHandlers): MarketSubscriptionHandle;
}

/** El mínimo de `ws` que usamos, para poder inyectar un socket falso en los tests. */
export interface WsLike {
  on(event: 'open' | 'message' | 'close' | 'error', cb: (...args: unknown[]) => void): void;
  send(data: string): void;
  close(): void;
}

export type WsFactory = (url: string) => WsLike;

export interface ClobMarketSocketOptions {
  url?: string;
  wsFactory?: WsFactory;
  logger?: Pick<Console, 'log' | 'warn' | 'error'>;
  pingIntervalMs?: number;
  reconnectDelayMs?: number;
}

/** El libro de un token, indexado por precio (string, para no perder exactitud). */
interface BookState {
  bids: Map<string, number>;
  asks: Map<string, number>;
}

export class ClobMarketSocket implements MarketFeed {
  private readonly url: string;
  private readonly wsFactory: WsFactory;
  private readonly log: Pick<Console, 'log' | 'warn' | 'error'>;
  private readonly pingIntervalMs: number;
  private readonly reconnectDelayMs: number;

  private ws: WsLike | null = null;
  private started = false;
  private closedByUs = false;

  private desiredTokens: string[] = [];
  private handlers: MarketFeedHandlers | null = null;
  private readonly books = new Map<string, BookState>();

  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(opts: ClobMarketSocketOptions = {}) {
    this.url = opts.url ?? CLOB_MARKET_WS_URL;
    this.wsFactory =
      opts.wsFactory ?? ((url: string) => new WebSocket(url) as unknown as WsLike);
    this.log = opts.logger ?? console;
    this.pingIntervalMs = opts.pingIntervalMs ?? PING_INTERVAL_MS;
    this.reconnectDelayMs = opts.reconnectDelayMs ?? RECONNECT_DELAY_MS;
  }

  connect(): void {
    this.started = true;
    // El socket real se abre al primer subscribeMarkets: sin tokens no hay nada
    // que pedir, y el server cierra una conexión sin suscripción.
  }

  disconnect(): void {
    this.started = false;
    this.closedByUs = true;
    this.clearTimers();
    this.desiredTokens = [];
    this.handlers = null;
    this.books.clear();
    this.closeSocket();
  }

  subscribeMarkets(tokenIds: string[], handlers: MarketFeedHandlers): MarketSubscriptionHandle {
    this.handlers = handlers;
    this.desiredTokens = [...tokenIds];
    // El canal `market` no tiene "unsubscribe" por token: para cambiar el set se
    // reabre la conexión con los assets nuevos. El universo cambia cada pocos
    // minutos, así que el coste es despreciable y el estado queda limpio.
    this.reopen();

    return {
      unsubscribe: () => {
        this.desiredTokens = [];
        this.handlers = null;
        this.books.clear();
        this.closeSocket();
      },
    };
  }

  // ------------------------------------------------------------------------
  // Ciclo del socket
  // ------------------------------------------------------------------------

  private reopen(): void {
    this.closeSocket();
    if (!this.started || this.desiredTokens.length === 0) return;

    this.closedByUs = false;
    const ws = this.wsFactory(this.url);
    this.ws = ws;

    ws.on('open', () => {
      this.sendSubscribe();
      this.startPing();
    });
    ws.on('message', (data: unknown) => this.onRaw(data));
    ws.on('error', (err: unknown) => {
      this.handlers?.onError?.(err instanceof Error ? err : new Error(String(err)));
    });
    ws.on('close', () => {
      this.clearPing();
      if (this.ws === ws) this.ws = null;
      // Caída inesperada: reintenta mientras sigamos vivos y con tokens que pedir.
      if (!this.closedByUs && this.started && this.desiredTokens.length > 0) {
        this.scheduleReconnect();
      }
    });
  }

  private sendSubscribe(): void {
    if (!this.ws || this.desiredTokens.length === 0) return;
    const msg = JSON.stringify({ assets_ids: this.desiredTokens, type: 'market' });
    try {
      this.ws.send(msg);
    } catch (err) {
      this.log.warn('[clob-market-socket] no pude enviar la suscripción', err);
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.books.clear(); // el snapshot llega de nuevo al reconectar
      this.reopen();
    }, this.reconnectDelayMs);
  }

  private startPing(): void {
    this.clearPing();
    this.pingTimer = setInterval(() => {
      try {
        this.ws?.send('PING');
      } catch {
        /* el 'close' se encargará de reconectar */
      }
    }, this.pingIntervalMs);
  }

  private clearPing(): void {
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.pingTimer = null;
  }

  private clearTimers(): void {
    this.clearPing();
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private closeSocket(): void {
    if (!this.ws) return;
    const ws = this.ws;
    this.ws = null;
    this.closedByUs = true;
    try {
      ws.close();
    } catch {
      /* ya estaba cerrado */
    }
    // El próximo reopen() vuelve a poner closedByUs en false.
  }

  // ------------------------------------------------------------------------
  // Parseo de mensajes. Público para que los tests lo ejerciten sin socket.
  // ------------------------------------------------------------------------

  /** Recibe lo que emite `ws` (Buffer | string | array de ellos) y lo despacha. */
  onRaw(data: unknown): void {
    const text = Array.isArray(data)
      ? data.map((d) => String(d)).join('')
      : typeof data === 'string'
        ? data
        : String(data);

    const trimmed = text.trim();
    // El heartbeat vuelve como 'PONG' en texto plano: no es JSON, se ignora.
    if (trimmed === 'PONG' || trimmed === 'PING' || trimmed.length === 0) return;

    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return; // un frame no-JSON no es asunto nuestro
    }

    const events = Array.isArray(parsed) ? parsed : [parsed];
    for (const ev of events) {
      this.handleEvent(ev as Record<string, unknown>);
    }
  }

  private handleEvent(ev: Record<string, unknown>): void {
    switch (ev.event_type) {
      case 'book':
        this.handleBook(ev);
        break;
      case 'price_change':
        this.handlePriceChange(ev);
        break;
      case 'last_trade_price':
        this.handleLastTrade(ev);
        break;
      default:
        // tick_size_change, best_bid_ask, new_market, market_resolved: no los
        // necesitamos para publicar el libro.
        break;
    }
  }

  private handleBook(ev: Record<string, unknown>): void {
    const assetId = ev.asset_id as string;
    if (!assetId) return;

    const state: BookState = { bids: new Map(), asks: new Map() };
    for (const lvl of asLevels(ev.bids)) state.bids.set(lvl.price, lvl.size);
    for (const lvl of asLevels(ev.asks)) state.asks.set(lvl.price, lvl.size);
    this.books.set(assetId, state);

    this.emitBook(assetId, ev.market as string | undefined, ev.hash as string | undefined);
  }

  private handlePriceChange(ev: Record<string, unknown>): void {
    const changes = ev.price_changes;
    if (!Array.isArray(changes)) return;

    const touched = new Set<string>();
    for (const raw of changes) {
      const c = raw as Record<string, unknown>;
      const assetId = c.asset_id as string;
      if (!assetId) continue;

      let state = this.books.get(assetId);
      if (!state) {
        state = { bids: new Map(), asks: new Map() };
        this.books.set(assetId, state);
      }

      const price = String(c.price);
      const size = Number(c.size);
      const side = String(c.side).toUpperCase();
      const levels = side === 'BUY' ? state.bids : state.asks;

      // size 0 = ese nivel se vació. Cualquier otro = tamaño absoluto del nivel.
      if (!Number.isFinite(size) || size <= 0) levels.delete(price);
      else levels.set(price, size);

      touched.add(assetId);
    }

    for (const assetId of touched) {
      this.emitBook(assetId, ev.market as string | undefined, undefined);
    }
  }

  private handleLastTrade(ev: Record<string, unknown>): void {
    const assetId = ev.asset_id as string;
    const price = Number(ev.price);
    if (!assetId || !Number.isFinite(price)) return;
    this.handlers?.onLastTrade?.({ assetId, price });
  }

  private emitBook(assetId: string, market: string | undefined, hash: string | undefined): void {
    const state = this.books.get(assetId);
    if (!state || !this.handlers?.onOrderbook) return;

    const snapshot: OrderbookSnapshot = {
      tokenId: assetId,
      assetId,
      market: market ?? '',
      bids: [...state.bids].map(([price, size]) => ({ price: Number(price), size })),
      asks: [...state.asks].map(([price, size]) => ({ price: Number(price), size })),
      timestamp: 0, // el publicador sella con su propio reloj; el WS no lo necesita
      tickSize: '0.01',
      minOrderSize: '0',
      hash: hash ?? '',
    };
    this.handlers.onOrderbook(snapshot);
  }
}

/** Normaliza `[{price,size}]` con valores string a números, tolerando basura. */
function asLevels(raw: unknown): Array<{ price: string; size: number }> {
  if (!Array.isArray(raw)) return [];
  const out: Array<{ price: string; size: number }> = [];
  for (const item of raw) {
    const lvl = item as Record<string, unknown>;
    if (lvl?.price === undefined || lvl?.size === undefined) continue;
    const size = Number(lvl.size);
    if (!Number.isFinite(size) || size <= 0) continue;
    out.push({ price: String(lvl.price), size });
  }
  return out;
}
