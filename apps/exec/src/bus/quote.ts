/**
 * Cotizar un intent contra la profundidad real del libro.
 *
 * Esto es el entregable de la Fase 3. `brain` predice el slippage con un modelo
 * de una línea (`execution/paper.py:_slippage_bps`): una base, más un factor por
 * el ratio nocional/liquidez, capado. `liquidity_num` viene de Gamma y es un
 * agregado que no dice nada sobre cómo están repartidas las órdenes. Aquí se
 * camina el libro de verdad, nivel a nivel, y sale el precio que el dinero
 * habría pagado. La resta entre los dos números es cuánto miente el backtest.
 *
 * Función pura: entra un libro y un intent, sale un fill. No hay Redis, ni
 * reloj, ni red. Todo lo que decide dinero se puede probar en una tabla.
 *
 * TRES COSAS QUE ESTA FUNCIÓN NO SIMULA, Y HAY QUE SABERLO AL LEER EL REPORTE
 * ---------------------------------------------------------------------------
 * 1. `GTC` se cotiza como `IOC`. Una orden que descansa en el libro se llena
 *    con el flujo que llegue después, y eso no está en el snapshot. Un `GTC`
 *    parcial aquí aparece como `PARTIAL`; en vivo podría acabar `FILLED` más
 *    tarde, o no llenarse nunca. El reporte de divergencia es, para `GTC`, una
 *    cota inferior del llenado.
 * 2. No hay impacto de mercado ni competencia. Se asume que el libro está quieto
 *    entre la foto y el llenado, y que nadie más se come los mismos niveles. En
 *    vivo, la parte de arriba del libro se mueve cuando la tocas.
 * 3. No hay latencia. El libro es el del instante del intent.
 *
 * Los tres sesgos apuntan al mismo lado: **esta cotización es optimista**. Si aun
 * así el slippage real se come el edge, el edge no existe.
 */

import {
  BPS_DENOM,
  SCALE,
  ZERO,
  div,
  formatFixed,
  mul,
  parseFixed,
  relativeBps,
} from './fixed.js';

/**
 * Los mínimos del CLOB de Polymarket. Se importan de `trading-service` en vez de
 * copiarlos: un intent que en vivo el exchange rechazaría no puede contar como
 * llenado en shadow, o el reporte mediría un mundo que no existe.
 */
import { MIN_ORDER_SIZE_SHARES, MIN_ORDER_VALUE_USDC } from '../services/trading-service.js';

export type QuoteSide = 'BUY' | 'SELL';
export type Tif = 'GTC' | 'FOK' | 'IOC';

/** Un nivel del libro. `number` porque así lo entrega `market-service`; ver `parseFixed`. */
export interface QuoteLevel {
  price: string | number;
  size: string | number;
}

export interface QuoteBook {
  bids: QuoteLevel[];
  asks: QuoteLevel[];
}

export interface QuoteRequest {
  side: QuoteSide;
  /** Presupuesto en dólares. Lo firmó el risk engine de brain; nunca se aumenta. */
  sizeUsd: string;
  limitPrice: string;
  tif: Tif;
  maxSlippageBps: number;
  /** Polymarket cobra 0% en casi todo. Configurable porque «casi» no es «siempre». */
  feeBps?: number;
}

export type QuoteStatus = 'FILLED' | 'PARTIAL' | 'REJECTED';

export interface Quote {
  status: QuoteStatus;
  filledShares: string;
  avgPrice: string;
  notionalUsd: string;
  feesUsd: string;
  /**
   * Positivo = adverso: pagaste por encima del mid comprando, cobraste por
   * debajo vendiendo. `null` solo si no hubo mid contra el que medir.
   *
   * Se rellena **también cuando el status es REJECTED por exceso de slippage**.
   * Que el libro real hubiera costado 450bps donde brain predijo 30 es
   * precisamente el dato que esta fase existe para recoger; tirarlo porque la
   * compuerta hizo su trabajo sería quedarse sin la mitad de la muestra.
   */
  realizedSlippageBps: number | null;
  /** El mid del token en el instante de la foto. Referencia del slippage. */
  midPrice: string | null;
  /** Vacío si no es REJECTED. Legible por humanos, no se parsea. */
  error: string;
}

/**
 * Por debajo de esto, el presupuesto restante es polvo de redondeo: un millonésimo
 * de dólar, el último decimal que la columna `Numeric(20,6)` puede guardar.
 */
const FILL_DUST_USD = 1n;

const MIN_SHARES = parseFixed(String(MIN_ORDER_SIZE_SHARES), 'down');
const MIN_NOTIONAL = parseFixed(String(MIN_ORDER_VALUE_USDC), 'down');

const REJECT: Omit<Quote, 'error' | 'realizedSlippageBps' | 'midPrice'> = {
  status: 'REJECTED',
  filledShares: '0.000000',
  avgPrice: '0.000000',
  notionalUsd: '0.000000',
  feesUsd: '0.000000',
};

function reject(error: string, realizedSlippageBps: number | null, midPrice: string | null): Quote {
  return { ...REJECT, error, realizedSlippageBps, midPrice };
}

interface ParsedLevel {
  price: bigint;
  size: bigint;
}

/**
 * Un lado del libro, ordenado como se consume y con el redondeo puesto en contra.
 *
 * El precio de un `ask` sube y el de un `bid` baja: en ambos casos es el precio
 * que de verdad te tocaría. Los tamaños siempre bajan — nunca damos por
 * disponible liquidez que el sexto decimal no garantiza. A los ticks de
 * Polymarket (0.01, 0.001) ningún redondeo llega a morder; muerde el día que
 * alguien publique un libro con más precisión de la que la base puede guardar.
 */
function parseSide(levels: QuoteLevel[], side: 'bid' | 'ask'): ParsedLevel[] {
  const parsed: ParsedLevel[] = [];
  for (const level of levels) {
    const price = parseFixed(level.price, side === 'ask' ? 'up' : 'down');
    const size = parseFixed(level.size, 'down');
    // Un nivel a precio cero o sin tamaño no es liquidez, es ruido del feed.
    if (price <= ZERO || price >= SCALE || size <= ZERO) continue;
    parsed.push({ price, size });
  }
  parsed.sort((a, b) => (side === 'bid' ? Number(b.price - a.price) : Number(a.price - b.price)));
  return parsed;
}

interface Walk {
  shares: bigint;
  notional: bigint;
  remaining: bigint;
}

/**
 * Consume niveles hasta agotar el presupuesto o quedarse sin libro dentro del límite.
 *
 * Compra y venta son la misma máquina con dos redondeos distintos: comprando, lo
 * que pagas por un nivel se redondea hacia arriba; vendiendo, lo que cobras se
 * redondea hacia abajo. Nunca al revés.
 */
function walk(
  levels: ParsedLevel[],
  side: QuoteSide,
  budgetUsd: bigint,
  limitPrice: bigint,
): Walk {
  const buying = side === 'BUY';
  const usdRounding = buying ? 'up' : 'down';

  let shares = ZERO;
  let notional = ZERO;
  let remaining = budgetUsd;

  for (const level of levels) {
    if (remaining <= ZERO) break;
    // El límite es una promesa: comprando no se paga por encima, vendiendo no se
    // cobra por debajo. Como el libro viene ordenado, el primer nivel que lo
    // cruza es el último que miramos.
    if (buying ? level.price > limitPrice : level.price < limitPrice) break;

    const wholeLevelUsd = mul(level.price, level.size, usdRounding);

    if (wholeLevelUsd <= remaining) {
      shares += level.size;
      notional += wholeLevelUsd;
      remaining -= wholeLevelUsd;
      continue;
    }

    // El nivel es más grande que lo que queda: se toma la parte que el dinero
    // alcanza, redondeada hacia abajo. Nunca se compran shares que no se pagaron.
    let take = div(remaining, level.price, 'down');
    let takeUsd = mul(level.price, take, usdRounding);
    // Redondear el coste hacia arriba puede empujarlo un millonésimo por encima
    // del presupuesto. Como el precio nunca supera 1.0, soltar una share lo
    // devuelve dentro: no hace falta iterar.
    if (takeUsd > remaining && take > ZERO) {
      take -= 1n;
      takeUsd = mul(level.price, take, usdRounding);
    }
    if (take <= ZERO) break;

    shares += take;
    notional += takeUsd;
    remaining -= takeUsd;
    break;
  }

  return { shares, notional, remaining };
}

/** El mid del token, o `null` si el libro solo tiene un lado o está cruzado. */
function midOf(bids: ParsedLevel[], asks: ParsedLevel[]): bigint | null {
  const bestBid = bids[0]?.price;
  const bestAsk = asks[0]?.price;
  if (bestBid === undefined || bestAsk === undefined) return null;
  // Un libro cruzado es un artefacto transitorio del feed. `book.ts` se niega a
  // publicarlo por la misma razón por la que aquí no se cotiza contra él.
  if (bestBid >= bestAsk) return null;
  return (bestBid + bestAsk) / 2n;
}

/**
 * Cotiza. Devuelve el fill que el libro habría dado, o por qué no lo habría dado.
 *
 * Un `REJECTED` no llena nada: `filledShares` y `notionalUsd` van a cero. Lo que
 * sí sobrevive al rechazo es la medición (`realizedSlippageBps`), porque es el
 * dato que se vino a buscar.
 */
export function quoteIntent(request: QuoteRequest, book: QuoteBook): Quote {
  const bids = parseSide(book.bids, 'bid');
  const asks = parseSide(book.asks, 'ask');

  const mid = midOf(bids, asks);
  const midPrice = mid === null ? null : formatFixed(mid);

  // Sin mid no hay contra qué medir, y medir es el único motivo de esta fase.
  // Un libro de un solo lado puede llenar una compra perfectamente; lo que no
  // puede es decirte cuánto te costó de más. Se rechaza con un motivo propio
  // para poder contarlo aparte en el reporte.
  if (mid === null) {
    return reject('sin mid: el libro está vacío, cruzado o tiene un solo lado', null, null);
  }

  const budget = parseFixed(request.sizeUsd, 'down');
  const limit = parseFixed(request.limitPrice, request.side === 'BUY' ? 'down' : 'up');
  if (budget <= ZERO) return reject('size_usd no positivo', null, midPrice);

  const levels = request.side === 'BUY' ? asks : bids;
  const { shares, notional, remaining } = walk(levels, request.side, budget, limit);

  if (shares <= ZERO) {
    return reject('sin liquidez dentro del limit_price', null, midPrice);
  }

  // Adverso también aquí: el precio medio sube comprando y baja vendiendo. El
  // nocional es la verdad; `avgPrice` es un resumen que no se usa para derivar
  // dinero, solo para medir y para que un humano lo lea.
  const avgPrice = div(notional, shares, request.side === 'BUY' ? 'up' : 'down');

  // Positivo = adverso, en los dos lados. Comprando, pagar por encima del mid;
  // vendiendo, cobrar por debajo.
  const rawBps = relativeBps(avgPrice, mid);
  const realizedSlippageBps = request.side === 'BUY' ? rawBps : -rawBps;

  const fullyFilled = remaining <= FILL_DUST_USD;

  if (request.tif === 'FOK' && !fullyFilled) {
    return reject(
      `FOK no llenable: el libro solo daba ${formatFixed(notional)} de ${request.sizeUsd}`,
      realizedSlippageBps,
      midPrice,
    );
  }

  // Los mínimos del exchange. Un fill que el CLOB habría rechazado no es un fill.
  if (shares < MIN_SHARES) {
    return reject(
      `por debajo del mínimo del CLOB: ${formatFixed(shares)} shares < ${MIN_ORDER_SIZE_SHARES}`,
      realizedSlippageBps,
      midPrice,
    );
  }
  if (notional < MIN_NOTIONAL) {
    return reject(
      `por debajo del mínimo del CLOB: $${formatFixed(notional)} < $${MIN_ORDER_VALUE_USDC}`,
      realizedSlippageBps,
      midPrice,
    );
  }

  if (realizedSlippageBps > request.maxSlippageBps) {
    return reject(
      `slippage ${realizedSlippageBps}bps > max_slippage_bps ${request.maxSlippageBps}`,
      realizedSlippageBps,
      midPrice,
    );
  }

  // Las fees se pagan: hacia arriba.
  const feeRate = (BigInt(request.feeBps ?? 0) * SCALE) / BPS_DENOM;
  const fees = mul(notional, feeRate, 'up');

  return {
    // `GTC` sin llenar del todo se reporta como `PARTIAL`. Ver la cabecera: el
    // resto de una orden que descansa no está en esta foto.
    status: fullyFilled ? 'FILLED' : 'PARTIAL',
    filledShares: formatFixed(shares),
    avgPrice: formatFixed(avgPrice),
    notionalUsd: formatFixed(notional),
    feesUsd: formatFixed(fees),
    realizedSlippageBps,
    midPrice,
    error: '',
  };
}
