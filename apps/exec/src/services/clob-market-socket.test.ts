import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ClobMarketSocket,
  type MarketFeedHandlers,
  type WsLike,
} from './clob-market-socket.js';
import type { OrderbookSnapshot } from './realtime-service-v2.js';

/** Socket de mentira: guarda lo enviado y deja disparar eventos a mano. */
class FakeWs implements WsLike {
  sent: string[] = [];
  closed = false;
  private cbs: Record<string, Array<(...a: unknown[]) => void>> = {};

  on(event: 'open' | 'message' | 'close' | 'error', cb: (...a: unknown[]) => void): void {
    (this.cbs[event] ??= []).push(cb);
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.closed = true;
    this.emit('close');
  }
  emit(event: string, ...args: unknown[]): void {
    for (const cb of this.cbs[event] ?? []) cb(...args);
  }
}

function setup(handlers: MarketFeedHandlers = {}) {
  const sockets: FakeWs[] = [];
  const socket = new ClobMarketSocket({
    wsFactory: () => {
      const ws = new FakeWs();
      sockets.push(ws);
      return ws;
    },
    logger: { log: () => {}, warn: () => {}, error: () => {} },
    pingIntervalMs: 10_000,
    reconnectDelayMs: 2_000,
  });
  const books: OrderbookSnapshot[] = [];
  const trades: Array<{ assetId: string; price: number }> = [];
  const h: MarketFeedHandlers = {
    onOrderbook: (b) => books.push(b),
    onLastTrade: (t) => trades.push(t),
    ...handlers,
  };
  return { socket, sockets, books, trades, h };
}

const msg = (obj: unknown) => JSON.stringify(obj);

afterEach(() => {
  vi.useRealTimers();
});

describe('ClobMarketSocket — suscripción', () => {
  it('manda la trama del canal `market` con los assets al abrir', () => {
    const { socket, sockets, h } = setup();
    socket.connect();
    socket.subscribeMarkets(['t1', 't2'], h);
    sockets[0].emit('open');

    expect(sockets[0].sent[0]).toBe(JSON.stringify({ assets_ids: ['t1', 't2'], type: 'market' }));
  });

  it('no abre socket sin tokens', () => {
    const { socket, sockets, h } = setup();
    socket.connect();
    socket.subscribeMarkets([], h);
    expect(sockets).toHaveLength(0);
  });
});

describe('ClobMarketSocket — parseo del libro', () => {
  it('un evento `book` se convierte en snapshot con niveles numéricos', () => {
    const { socket, sockets, books, h } = setup();
    socket.connect();
    socket.subscribeMarkets(['t1'], h);
    sockets[0].emit('open');

    sockets[0].emit(
      'message',
      msg({
        event_type: 'book',
        asset_id: 't1',
        market: '0xabc',
        bids: [{ price: '0.61', size: '100' }],
        asks: [{ price: '0.62', size: '50' }],
        hash: 'h1',
      }),
    );

    expect(books).toHaveLength(1);
    expect(books[0].assetId).toBe('t1');
    expect(books[0].market).toBe('0xabc');
    expect(books[0].bids).toEqual([{ price: 0.61, size: 100 }]);
    expect(books[0].asks).toEqual([{ price: 0.62, size: 50 }]);
  });

  it('acepta el payload como Buffer, no solo string', () => {
    const { socket, sockets, books, h } = setup();
    socket.connect();
    socket.subscribeMarkets(['t1'], h);
    sockets[0].emit('open');

    sockets[0].emit(
      'message',
      Buffer.from(msg({ event_type: 'book', asset_id: 't1', bids: [{ price: '0.5', size: '10' }], asks: [] })),
    );
    expect(books.at(-1)!.bids).toEqual([{ price: 0.5, size: 10 }]);
  });

  it('un `price_change` se aplica sobre el snapshot vigente', () => {
    const { socket, sockets, books, h } = setup();
    socket.connect();
    socket.subscribeMarkets(['t1'], h);
    sockets[0].emit('open');

    sockets[0].emit(
      'message',
      msg({
        event_type: 'book',
        asset_id: 't1',
        bids: [{ price: '0.61', size: '100' }],
        asks: [{ price: '0.62', size: '50' }],
      }),
    );
    sockets[0].emit(
      'message',
      msg({
        event_type: 'price_change',
        price_changes: [{ asset_id: 't1', price: '0.60', size: '80', side: 'BUY' }],
      }),
    );

    const last = books.at(-1)!;
    const bidMap = Object.fromEntries(last.bids.map((l) => [l.price, l.size]));
    expect(bidMap).toEqual({ 0.61: 100, 0.6: 80 });
    expect(last.asks).toEqual([{ price: 0.62, size: 50 }]);
  });

  it('un `price_change` con size 0 borra el nivel', () => {
    const { socket, sockets, books, h } = setup();
    socket.connect();
    socket.subscribeMarkets(['t1'], h);
    sockets[0].emit('open');

    sockets[0].emit('message', msg({ event_type: 'book', asset_id: 't1', bids: [{ price: '0.61', size: '100' }], asks: [] }));
    sockets[0].emit('message', msg({ event_type: 'price_change', price_changes: [{ asset_id: 't1', price: '0.61', size: '0', side: 'BUY' }] }));

    expect(books.at(-1)!.bids).toEqual([]);
  });

  it('emite el último trade con assetId y precio', () => {
    const { socket, sockets, trades, h } = setup();
    socket.connect();
    socket.subscribeMarkets(['t1'], h);
    sockets[0].emit('open');

    sockets[0].emit('message', msg({ event_type: 'last_trade_price', asset_id: 't1', price: '0.615', size: '5', side: 'BUY' }));
    expect(trades).toEqual([{ assetId: 't1', price: 0.615 }]);
  });

  it('ignora PONG y frames no-JSON sin romperse', () => {
    const { socket, sockets, books, trades, h } = setup();
    socket.connect();
    socket.subscribeMarkets(['t1'], h);
    sockets[0].emit('open');

    expect(() => {
      sockets[0].emit('message', 'PONG');
      sockets[0].emit('message', 'no soy json {');
    }).not.toThrow();
    expect(books).toHaveLength(0);
    expect(trades).toHaveLength(0);
  });
});

describe('ClobMarketSocket — ciclo de vida', () => {
  it('reconecta y re-suscribe tras una caída inesperada', () => {
    vi.useFakeTimers();
    const { socket, sockets, h } = setup();
    socket.connect();
    socket.subscribeMarkets(['t1'], h);
    sockets[0].emit('open');
    expect(sockets[0].sent).toHaveLength(1);

    // El server cierra la conexión sin que se lo pidiéramos.
    sockets[0].emit('close');
    vi.advanceTimersByTime(2_000);

    // Se abrió un socket nuevo; al abrir, re-manda la suscripción.
    expect(sockets).toHaveLength(2);
    sockets[1].emit('open');
    expect(sockets[1].sent[0]).toBe(JSON.stringify({ assets_ids: ['t1'], type: 'market' }));
  });

  it('disconnect() cierra el socket y no reconecta', () => {
    vi.useFakeTimers();
    const { socket, sockets, h } = setup();
    socket.connect();
    socket.subscribeMarkets(['t1'], h);
    sockets[0].emit('open');

    socket.disconnect();
    expect(sockets[0].closed).toBe(true);
    vi.advanceTimersByTime(10_000);
    expect(sockets).toHaveLength(1); // no hubo reintento
  });

  it('unsubscribe() deja de emitir libros', () => {
    const { socket, sockets, books, h } = setup();
    socket.connect();
    const sub = socket.subscribeMarkets(['t1'], h);
    sockets[0].emit('open');
    sub.unsubscribe();

    // Un book que llegue tras el unsubscribe (socket ya cerrado) no debe emitir.
    sockets[0].emit('message', msg({ event_type: 'book', asset_id: 't1', bids: [{ price: '0.5', size: '1' }], asks: [] }));
    expect(books).toHaveLength(0);
  });
});
