import { describe, expect, it } from 'vitest';
import { quoteIntent, type QuoteBook, type QuoteRequest } from './quote.js';

/** Un libro con dos lados y spread de 1 centavo. Mid = 0.615. */
function book(over: Partial<QuoteBook> = {}): QuoteBook {
  return {
    bids: [
      { price: '0.61', size: '1200' },
      { price: '0.60', size: '800' },
    ],
    asks: [
      { price: '0.62', size: '100' },
      { price: '0.63', size: '200' },
    ],
    ...over,
  };
}

function buy(over: Partial<QuoteRequest> = {}): QuoteRequest {
  return {
    side: 'BUY',
    sizeUsd: '100',
    limitPrice: '0.99',
    tif: 'IOC',
    maxSlippageBps: 1000,
    ...over,
  };
}

describe('quoteIntent — el libro, nivel a nivel', () => {
  it('come un solo nivel cuando el dinero cabe en él', () => {
    const q = quoteIntent(buy({ sizeUsd: '62' }), book());
    expect(q.status).toBe('FILLED');
    expect(q.filledShares).toBe('100.000000');
    expect(q.notionalUsd).toBe('62.000000');
    expect(q.avgPrice).toBe('0.620000');
    expect(q.midPrice).toBe('0.615000');
    // (0.62 - 0.615) / 0.615 = 81.3bps
    expect(q.realizedSlippageBps).toBe(81);
  });

  it('atraviesa varios niveles y el precio medio sube con la profundidad', () => {
    // $62 se comen el nivel de 0.62; los $38 restantes suben a 0.63.
    const q = quoteIntent(buy({ sizeUsd: '100' }), book());
    expect(q.status).toBe('FILLED');
    expect(q.notionalUsd).toBe('100.000000');
    expect(q.filledShares).toBe('160.317460');
    expect(q.avgPrice).toBe('0.623763');
    // Más caro que tocar solo el primer nivel: esto es lo que el modelo de brain no ve.
    expect(q.realizedSlippageBps).toBe(142);
  });

  it('el limit_price detiene el paseo, aunque quede libro', () => {
    const q = quoteIntent(buy({ sizeUsd: '100', limitPrice: '0.62' }), book());
    // Solo el nivel de 0.62 es elegible: 100 shares, $62. El resto no se llena.
    expect(q.status).toBe('PARTIAL');
    expect(q.filledShares).toBe('100.000000');
    expect(q.notionalUsd).toBe('62.000000');
  });

  it('nunca gasta más del presupuesto que firmó el risk engine', () => {
    for (const sizeUsd of ['1', '7.5', '61.999999', '100', '150']) {
      const q = quoteIntent(buy({ sizeUsd }), book());
      if (q.status === 'REJECTED') continue;
      expect(Number(q.notionalUsd)).toBeLessThanOrEqual(Number(sizeUsd));
    }
  });
});

describe('quoteIntent — time in force', () => {
  const thin: QuoteBook = { bids: [{ price: '0.61', size: '1200' }], asks: [{ price: '0.62', size: '100' }] };

  it('FOK se rechaza entero si el libro no lo llena entero', () => {
    const q = quoteIntent(buy({ sizeUsd: '100', tif: 'FOK' }), thin);
    expect(q.status).toBe('REJECTED');
    expect(q.filledShares).toBe('0.000000');
    expect(q.notionalUsd).toBe('0.000000');
    expect(q.error).toContain('FOK no llenable');
    // El rechazo no tira la medición.
    expect(q.realizedSlippageBps).toBe(81);
  });

  it('FOK pasa si el libro lo llena entero', () => {
    const q = quoteIntent(buy({ sizeUsd: '62', tif: 'FOK' }), thin);
    expect(q.status).toBe('FILLED');
  });

  it('IOC llena lo que puede y reporta PARTIAL', () => {
    const q = quoteIntent(buy({ sizeUsd: '100', tif: 'IOC' }), thin);
    expect(q.status).toBe('PARTIAL');
    expect(q.notionalUsd).toBe('62.000000');
  });

  it('GTC se cota como IOC: el resto que descansaría no está en la foto', () => {
    const gtc = quoteIntent(buy({ sizeUsd: '100', tif: 'GTC' }), thin);
    const ioc = quoteIntent(buy({ sizeUsd: '100', tif: 'IOC' }), thin);
    expect(gtc).toEqual(ioc);
    expect(gtc.status).toBe('PARTIAL');
  });
});

describe('quoteIntent — las compuertas', () => {
  it('rechaza por slippage, y se queda con el número que lo delató', () => {
    const q = quoteIntent(buy({ sizeUsd: '100', maxSlippageBps: 100 }), book());
    expect(q.status).toBe('REJECTED');
    expect(q.error).toContain('slippage 142bps > max_slippage_bps 100');
    // Este es el dato de la Fase 3: el libro real habría costado 142bps.
    expect(q.realizedSlippageBps).toBe(142);
    expect(q.filledShares).toBe('0.000000');
  });

  it('acepta justo en el límite', () => {
    const q = quoteIntent(buy({ sizeUsd: '100', maxSlippageBps: 142 }), book());
    expect(q.status).toBe('FILLED');
  });

  it('sin mid no cotiza: un libro de un solo lado no dice cuánto costó de más', () => {
    const q = quoteIntent(buy(), book({ bids: [] }));
    expect(q.status).toBe('REJECTED');
    expect(q.error).toContain('sin mid');
    expect(q.midPrice).toBeNull();
    expect(q.realizedSlippageBps).toBeNull();
  });

  it('no cotiza contra un libro cruzado', () => {
    const q = quoteIntent(buy(), book({ bids: [{ price: '0.63', size: '10' }] }));
    expect(q.status).toBe('REJECTED');
    expect(q.error).toContain('sin mid');
  });

  it('respeta el mínimo de shares del CLOB', () => {
    // $2 a 0.62 son ~3.2 shares. El exchange no habría aceptado la orden.
    const q = quoteIntent(buy({ sizeUsd: '2' }), book());
    expect(q.status).toBe('REJECTED');
    expect(q.error).toContain('mínimo del CLOB');
    expect(q.error).toContain('shares');
  });

  it('respeta el mínimo de nocional del CLOB', () => {
    // 9 shares a $0.10 son $0.90: pasa el mínimo de shares, no el de dinero.
    const cheap: QuoteBook = { bids: [{ price: '0.09', size: '1000' }], asks: [{ price: '0.10', size: '1000' }] };
    const q = quoteIntent(buy({ sizeUsd: '0.9' }), cheap);
    expect(q.status).toBe('REJECTED');
    expect(q.error).toContain('$0.900000 < $1');
  });

  it('rechaza cuando no hay liquidez dentro del límite', () => {
    const q = quoteIntent(buy({ limitPrice: '0.50' }), book());
    expect(q.status).toBe('REJECTED');
    expect(q.error).toContain('sin liquidez dentro del limit_price');
  });

  it('rechaza un size_usd no positivo', () => {
    const q = quoteIntent(buy({ sizeUsd: '0' }), book());
    expect(q.status).toBe('REJECTED');
    expect(q.error).toContain('no positivo');
  });
});

describe('quoteIntent — vender', () => {
  const sell = (over: Partial<QuoteRequest> = {}): QuoteRequest => ({
    side: 'SELL',
    sizeUsd: '30',
    limitPrice: '0.01',
    tif: 'IOC',
    maxSlippageBps: 1000,
    ...over,
  });

  it('cobra contra los bids, y el slippage adverso sigue siendo positivo', () => {
    const q = quoteIntent(sell(), book());
    expect(q.status).toBe('FILLED');
    // $30 / 0.61 = 49.180327 shares (hacia abajo: no se cobran shares que no se vendieron)
    expect(q.filledShares).toBe('49.180327');
    // 0.609999, no 0.610000: el nocional ya venía redondeado a la baja y el precio
    // medio se redondea a la baja otra vez. Un millonésimo, siempre en contra nuestra.
    expect(q.avgPrice).toBe('0.609999');
    // Cobrar 0.61 con el mid en 0.615 es adverso: +81bps, mismo signo que comprando caro.
    expect(q.realizedSlippageBps).toBe(81);
  });

  it('el limit_price le pone suelo a lo que se acepta cobrar', () => {
    const q = quoteIntent(sell({ sizeUsd: '2000', limitPrice: '0.61' }), book());
    // Solo el bid de 0.61 (1200 shares = $732) es elegible. El de 0.60 no.
    expect(q.status).toBe('PARTIAL');
    expect(q.notionalUsd).toBe('732.000000');
    expect(q.filledShares).toBe('1200.000000');
  });

  it('atraviesa bids y el precio medio baja', () => {
    const q = quoteIntent(sell({ sizeUsd: '800' }), book());
    expect(q.status).toBe('FILLED');
    expect(Number(q.avgPrice)).toBeLessThan(0.61);
    expect(Number(q.avgPrice)).toBeGreaterThan(0.6);
    expect(q.realizedSlippageBps).toBeGreaterThan(81);
  });
});

describe('quoteIntent — fees y redondeo', () => {
  it('las fees se pagan: hacia arriba', () => {
    const q = quoteIntent(buy({ sizeUsd: '62', feeBps: 1 }), book());
    // 62 * 0.0001 = 0.0062 exacto.
    expect(q.feesUsd).toBe('0.006200');
  });

  it('sin fee_bps, no hay fees', () => {
    expect(quoteIntent(buy({ sizeUsd: '62' }), book()).feesUsd).toBe('0.000000');
  });

  it('ignora los niveles que no son liquidez', () => {
    const noisy = book({
      asks: [
        { price: '0', size: '999' },
        { price: '1', size: '999' },
        { price: '0.62', size: '0' },
        { price: '0.63', size: '200' },
      ],
    });
    const q = quoteIntent(buy({ sizeUsd: '63' }), noisy);
    expect(q.status).toBe('FILLED');
    expect(q.avgPrice).toBe('0.630000');
  });

  it('ordena el libro aunque llegue desordenado', () => {
    const shuffled = book({
      asks: [
        { price: '0.63', size: '200' },
        { price: '0.62', size: '100' },
      ],
    });
    expect(quoteIntent(buy({ sizeUsd: '100' }), shuffled)).toEqual(
      quoteIntent(buy({ sizeUsd: '100' }), book()),
    );
  });
});
