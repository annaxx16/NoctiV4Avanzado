import { describe, expect, it } from 'vitest';
import type { OrderbookSnapshot } from '../services/realtime-service-v2.js';
import {
  MAX_LEVELS,
  SOURCE_CLOB_WS,
  bookKey,
  buildCachedBook,
  decodeUniverse,
  yesTokenId,
  type UniverseMarket,
} from './book.js';

const CID = '0x' + 'ab'.repeat(32);
const TS = new Date('2026-07-08T12:00:00.000Z');

const market: UniverseMarket = {
  condition_id: CID,
  rank: 1,
  token_ids: ['tok_yes', 'tok_no'],
  yes_token_id: 'tok_yes',
  liquidity_num: 12_000,
  volume_24hr: 55_000,
};

function ob(over: Partial<OrderbookSnapshot> = {}): OrderbookSnapshot {
  return {
    tokenId: 'tok_yes',
    assetId: 'tok_yes',
    market: CID,
    tickSize: '0.01',
    minOrderSize: '5',
    hash: 'h',
    timestamp: TS.getTime(),
    bids: [
      { price: 0.61, size: 1200 },
      { price: 0.6, size: 800 },
    ],
    asks: [{ price: 0.62, size: 950 }],
    ...over,
  } as OrderbookSnapshot;
}

function build(over: Partial<OrderbookSnapshot> = {}, lastTradePrice: number | null = 0.615) {
  return buildCachedBook({ market, book: ob(over), lastTradePrice, ts: TS });
}

describe('buildCachedBook', () => {
  it('compone la forma exacta que brain ya lee', () => {
    const r = build();
    expect(r.ok).toBe(true);
    if (!r.ok) return;

    expect(r.book).toEqual({
      condition_id: CID,
      ts: '2026-07-08T12:00:00.000Z',
      best_bid: 0.61,
      best_ask: 0.62,
      last_trade_price: 0.615,
      spread: 0.01,
      liquidity_num: 12_000,
      volume_24hr: 55_000,
      bids: [
        ['0.61', '1200'],
        ['0.6', '800'],
      ],
      asks: [['0.62', '950']],
      source: SOURCE_CLOB_WS,
    });
  });

  it('el spread no arrastra basura de coma flotante', () => {
    // 0.62 - 0.61 === 0.010000000000000009 en IEEE-754
    const r = build();
    if (!r.ok) throw new Error('esperaba ok');
    expect(r.book.spread).toBe(0.01);
  });

  it('se niega a publicar un libro cruzado', () => {
    // Publicarlo daría a brain un spread negativo, y el spread alimenta la
    // compuerta de liquidez del risk engine.
    const r = build({ bids: [{ price: 0.7, size: 10 }], asks: [{ price: 0.65, size: 10 }] });
    expect(r).toEqual({ ok: false, reason: 'crossed' });
  });

  it('se niega a publicar un libro con bid igual a ask', () => {
    const r = build({ bids: [{ price: 0.5, size: 10 }], asks: [{ price: 0.5, size: 10 }] });
    expect(r).toEqual({ ok: false, reason: 'crossed' });
  });

  it('se niega a publicar un libro vacío', () => {
    expect(build({ bids: [], asks: [] })).toEqual({ ok: false, reason: 'empty' });
  });

  it('un libro con un solo lado es información, no ruido', () => {
    const r = build({ bids: [] });
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.book.best_bid).toBeNull();
    expect(r.book.best_ask).toBe(0.62);
    expect(r.book.spread).toBeNull();
  });

  it('descarta niveles con precio fuera de (0,1) y tamaño cero', () => {
    // 0 y 1 solo aparecen en mercados resueltos, donde no hay libro que publicar.
    const r = build({
      bids: [
        { price: 0, size: 100 },
        { price: 0.61, size: 0 },
        { price: 0.6, size: 800 },
      ],
      asks: [
        { price: 1, size: 100 },
        { price: 0.62, size: 950 },
      ],
    });
    if (!r.ok) throw new Error('esperaba ok');
    expect(r.book.bids).toEqual([['0.6', '800']]);
    expect(r.book.asks).toEqual([['0.62', '950']]);
  });

  it('ordena los niveles aunque el feed los mande desordenados', () => {
    const r = build({
      bids: [
        { price: 0.55, size: 1 },
        { price: 0.61, size: 2 },
        { price: 0.58, size: 3 },
      ],
      asks: [
        { price: 0.66, size: 1 },
        { price: 0.62, size: 2 },
      ],
    });
    if (!r.ok) throw new Error('esperaba ok');
    expect(r.book.bids.map((l) => l[0])).toEqual(['0.61', '0.58', '0.55']);
    expect(r.book.asks.map((l) => l[0])).toEqual(['0.62', '0.66']);
    expect(r.book.best_bid).toBe(0.61);
  });

  it(`trunca a ${MAX_LEVELS} niveles por lado`, () => {
    const bids = Array.from({ length: 40 }, (_, i) => ({ price: 0.5 - i * 0.001, size: 10 }));
    const r = build({ bids });
    if (!r.ok) throw new Error('esperaba ok');
    expect(r.book.bids).toHaveLength(MAX_LEVELS);
    expect(r.book.bids[0][0]).toBe('0.5'); // el mejor sobrevive
  });

  it('last_trade_price es null hasta que el WebSocket ve el primer trade', () => {
    const r = build({}, null);
    if (!r.ok) throw new Error('esperaba ok');
    expect(r.book.last_trade_price).toBeNull();
  });

  it('liquidez y volumen vienen del universo, no del WebSocket', () => {
    const r = build();
    if (!r.ok) throw new Error('esperaba ok');
    expect(r.book.liquidity_num).toBe(12_000);
    expect(r.book.volume_24hr).toBe(55_000);
  });
});

describe('universo', () => {
  it('usa el token de YES que brain resolvió, no la posición en el array', () => {
    // Si Gamma invierte `outcomes`, token_ids[0] es el NO. Fiarse de la posición
    // publicaría el libro equivocado y brain vería los precios invertidos.
    const invertido: UniverseMarket = {
      ...market,
      token_ids: ['tok_no', 'tok_yes'],
      yes_token_id: 'tok_yes',
    };
    expect(yesTokenId(invertido)).toBe('tok_yes');
  });

  it('un mercado sin YES identificable no se vigila', () => {
    expect(yesTokenId({ ...market, yes_token_id: null })).toBeNull();
  });

  it('decodifica tolerando campos que exec no conoce', () => {
    const raw = JSON.stringify({
      ts: TS.toISOString(),
      markets: [{ ...market, campo_del_futuro: 1 }],
    });
    expect(decodeUniverse(raw).markets[0].condition_id).toBe(CID);
  });

  it('un universo sin `markets` es un error, no un universo vacío', () => {
    // Tragárselo en silencio dejaría a exec sin publicar y sin que nadie lo sepa.
    expect(() => decodeUniverse('{"ts":"x"}')).toThrow(/malformado/);
  });
});

describe('bookKey', () => {
  it('coincide con la clave que brain lee', () => {
    expect(bookKey(CID)).toBe(`book:${CID}`);
  });
});
