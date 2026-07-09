/**
 * El contrato del book, del lado de exec.
 *
 * brain lee `book:{condition_id}` desde antes de la fusión y no sabe que ahora
 * se lo escribimos nosotros. La forma del JSON es sagrada: si cambia, brain se
 * queda ciego sin avisar. Ver packages/contracts/book.schema.json.
 *
 * Precios y tamaños viajan como string. Un float de 64 bits no representa 0.62
 * exactamente, y aquí eso es dinero.
 */

import type { OrderbookSnapshot } from '../services/realtime-service-v2.js';

export const BOOK_KEY_PREFIX = 'book:';
export const BOOK_TTL_SEC = 60;
export const UNIVERSE_KEY = 'nocti:universe';

export const SOURCE_CLOB_WS = 'clob_ws';

/** Niveles publicados por lado. Más allá, nadie los mira y ocupan Redis. */
export const MAX_LEVELS = 20;

export type Level = [price: string, size: string];

/** Un mercado del universo, publicado por brain. exec no habla con Postgres. */
export interface UniverseMarket {
  condition_id: string;
  rank: number;
  /** Token IDs del CTF, en el orden de `outcomes`. */
  token_ids: string[];
  /**
   * El token del outcome YES, resuelto por brain contra `outcomes`.
   *
   * Null si no hay un YES identificable. No lo adivinamos: publicar el libro del
   * NO como si fuera el del mercado invertiría todos los precios que ve brain,
   * en silencio y sin que nada fallara.
   */
  yes_token_id: string | null;
  liquidity_num: number | null;
  volume_24hr: number | null;
}

export interface Universe {
  ts: string;
  markets: UniverseMarket[];
}

export interface CachedBook {
  condition_id: string;
  ts: string;
  best_bid: number | null;
  best_ask: number | null;
  last_trade_price: number | null;
  spread: number | null;
  liquidity_num: number | null;
  volume_24hr: number | null;
  bids: Level[];
  asks: Level[];
  source: string;
}

export function bookKey(conditionId: string): string {
  return `${BOOK_KEY_PREFIX}${conditionId}`;
}

export function decodeUniverse(raw: string): Universe {
  const data = JSON.parse(raw) as Universe;
  if (!Array.isArray(data?.markets)) {
    throw new Error('universo malformado: falta `markets`');
  }
  return data;
}

/**
 * El token de YES, que es el lado que Gamma reporta a nivel de mercado y el que
 * brain asume en todo su pipeline.
 *
 * Lo resuelve brain, que conoce `outcomes`. Si no viene, este mercado no se
 * vigila: mejor un hueco que un precio invertido.
 */
export function yesTokenId(market: UniverseMarket): string | null {
  return market.yes_token_id ?? null;
}

/**
 * Un precio de Polymarket es una probabilidad: vive en (0, 1).
 * Exactamente 0 o 1 solo aparecen cuando el mercado ya está resuelto, y entonces
 * no hay libro que publicar.
 */
function isValidPrice(p: number | undefined): p is number {
  return typeof p === 'number' && Number.isFinite(p) && p > 0 && p < 1;
}

/**
 * `0.62 - 0.61` da `0.010000000000000009` en coma flotante. La columna de brain
 * es Numeric(12,6), así que el redondeo ocurre igual: mejor aquí, y explícito.
 */
function round6(n: number): number {
  return Math.round(n * 1e6) / 1e6;
}

function toLevels(
  levels: Array<{ price: number; size: number }> | undefined,
  side: 'bid' | 'ask',
): Level[] {
  if (!levels?.length) return [];
  const clean = levels.filter((l) => isValidPrice(l.price) && l.size > 0);
  // El WS los manda ordenados, pero no lo damos por hecho: un libro mal ordenado
  // haría que `best_bid` fuese cualquier cosa.
  clean.sort((a, b) => (side === 'bid' ? b.price - a.price : a.price - b.price));
  return clean.slice(0, MAX_LEVELS).map((l) => [String(l.price), String(l.size)]);
}

export interface BuildBookArgs {
  market: UniverseMarket;
  book: OrderbookSnapshot;
  /** Del canal `last_trade_price`. Null hasta que el WS vea el primer trade. */
  lastTradePrice: number | null;
  ts: Date;
}

export type BuildBookResult =
  | { ok: true; book: CachedBook }
  | { ok: false; reason: 'empty' | 'crossed' };

/**
 * Compone el book publicable, o explica por qué no lo es.
 *
 * Un libro cruzado (bid >= ask) es casi siempre un artefacto transitorio del
 * feed. Publicarlo le daría a brain un spread negativo, y el spread alimenta la
 * compuerta de liquidez del risk engine. Preferimos no publicar: el book anterior
 * caduca a los 60s y el poller vuelve a Gamma por su cuenta. Degradar es barato;
 * mentir, no.
 */
export function buildCachedBook({ market, book, lastTradePrice, ts }: BuildBookArgs): BuildBookResult {
  const bids = toLevels(book.bids, 'bid');
  const asks = toLevels(book.asks, 'ask');

  if (!bids.length && !asks.length) {
    return { ok: false, reason: 'empty' };
  }

  const bestBid = bids.length ? Number(bids[0][0]) : null;
  const bestAsk = asks.length ? Number(asks[0][0]) : null;

  if (bestBid !== null && bestAsk !== null && bestBid >= bestAsk) {
    return { ok: false, reason: 'crossed' };
  }

  const spread = bestBid !== null && bestAsk !== null ? round6(bestAsk - bestBid) : null;

  return {
    ok: true,
    book: {
      condition_id: market.condition_id,
      ts: ts.toISOString(),
      best_bid: bestBid,
      best_ask: bestAsk,
      last_trade_price: lastTradePrice,
      spread,
      // El WebSocket no los conoce. Viajan en el universo que publica brain,
      // que sí habla con Gamma. Así exec no necesita llamar a Gamma nunca.
      liquidity_num: market.liquidity_num,
      volume_24hr: market.volume_24hr,
      bids,
      asks,
      source: SOURCE_CLOB_WS,
    },
  };
}
