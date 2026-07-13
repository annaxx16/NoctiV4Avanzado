/**
 * De dónde sale el libro contra el que se cotiza un intent.
 *
 * No se reusa `book:{condition_id}` de Redis, y la razón es de correción, no de
 * frescura: esa clave lleva **el libro del token YES**, uno por mercado. Un intent
 * sobre el token NO cotizado contra el libro del YES daría un precio invertido,
 * en silencio y sin que nada fallara. Es el mismo error que `yes_token_id` evita
 * en la Fase 1, visto desde el otro lado.
 *
 * Así que se pide el libro del token exacto que dice el intent. El cliente del
 * CLOB va **sin wallet**: `getOrderBook` es un endpoint público. De ahí una
 * propiedad que conviene no perder de vista:
 *
 *   el camino shadow entero no necesita una clave privada.
 *
 * Un proceso que no puede firmar no puede firmar por error. Mientras la Fase 3
 * dure, `exec` puede correr sin `PRIVATE_KEY` en el entorno.
 *
 * Se habla con `ClobClient` directamente, sin pasar por `market-service`, para no
 * arrastrar la caché y el rate limiter, y sobre todo para no dejar que un
 * `parseFloat` toque los precios: aquí llegan como strings y como strings entran
 * en la aritmética de `fixed.ts`.
 */

import { ClobClient, type Chain, type OrderBookSummary } from '@polymarket/clob-client';
import type { QuoteBook } from './quote.js';

export const CLOB_HOST = 'https://clob.polymarket.com';
export const POLYGON_MAINNET = 137;

export interface BookSource {
  fetch(tokenId: string): Promise<QuoteBook>;
}

export class ClobBookSource implements BookSource {
  private readonly client: ClobClient;

  constructor(host: string = CLOB_HOST, chainId: number = POLYGON_MAINNET) {
    // Sin wallet: cliente de solo lectura.
    this.client = new ClobClient(host, chainId as Chain);
  }

  async fetch(tokenId: string): Promise<QuoteBook> {
    const book = (await this.client.getOrderBook(tokenId)) as OrderBookSummary;
    // Sin ordenar y sin filtrar: de eso se encarga `quoteIntent`, que es la que
    // se prueba. Aquí solo se traduce la forma.
    return {
      bids: book.bids ?? [],
      asks: book.asks ?? [],
    };
  }
}
