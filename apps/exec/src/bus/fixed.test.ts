import { describe, expect, it } from 'vitest';
import { SCALE, div, formatFixed, mul, parseFixed, relativeBps } from './fixed.js';

describe('parseFixed', () => {
  it('convierte decimales posicionales sin pasar por float', () => {
    expect(parseFixed('0.62', 'down')).toBe(620_000n);
    expect(parseFixed('1', 'down')).toBe(1_000_000n);
    expect(parseFixed('0', 'down')).toBe(0n);
    expect(parseFixed('1234.5', 'down')).toBe(1_234_500_000n);
  });

  it('acepta el float que entrega market-service, y lo mata en la puerta', () => {
    // `String(0.62)` es "0.62": JS imprime el decimal más corto que round-trippea.
    expect(parseFixed(0.62, 'down')).toBe(620_000n);
    expect(parseFixed(0.1 + 0.2, 'down')).toBe(300_000n);
  });

  it('redondea solo si de verdad se perdió algo', () => {
    expect(parseFixed('0.1234565', 'up')).toBe(123_457n);
    expect(parseFixed('0.1234565', 'down')).toBe(123_456n);
    // Séptimo decimal a cero: no hay nada que redondear, `up` no inventa un ulp.
    expect(parseFixed('0.1234560', 'up')).toBe(123_456n);
    expect(parseFixed('0.123456', 'up')).toBe(123_456n);
  });

  it('grita en vez de truncar lo que no sabe leer', () => {
    expect(() => parseFixed('-1', 'down')).toThrow(RangeError);
    expect(() => parseFixed('1e-7', 'down')).toThrow(RangeError);
    expect(() => parseFixed(Number.NaN, 'down')).toThrow(RangeError);
    expect(() => parseFixed(Number.POSITIVE_INFINITY, 'down')).toThrow(RangeError);
    expect(() => parseFixed('', 'down')).toThrow(RangeError);
  });
});

describe('formatFixed', () => {
  it('siempre seis decimales: es la escala de la columna de Postgres', () => {
    expect(formatFixed(620_000n)).toBe('0.620000');
    expect(formatFixed(0n)).toBe('0.000000');
    expect(formatFixed(1_000_000n)).toBe('1.000000');
    expect(formatFixed(1n)).toBe('0.000001');
  });

  it('round-trippea con parseFixed', () => {
    for (const s of ['0.000001', '0.623762', '100.000000', '1234.567890']) {
      expect(formatFixed(parseFixed(s, 'down'))).toBe(s);
    }
  });
});

describe('mul y div', () => {
  it('corrigen la escala', () => {
    // 0.62 * 100 = 62
    expect(mul(620_000n, 100n * SCALE, 'down')).toBe(62n * SCALE);
    // 62 / 0.62 = 100
    expect(div(62n * SCALE, 620_000n, 'down')).toBe(100n * SCALE);
  });

  it('el redondeo cae donde se le pide, y solo si hay resto', () => {
    // 0.333333 * 0.000001 → 3.33333e-13, todo por debajo de la escala.
    expect(mul(333_333n, 1n, 'down')).toBe(0n);
    expect(mul(333_333n, 1n, 'up')).toBe(1n);
    // Exacto: `up` no añade un ulp de la nada.
    expect(mul(500_000n, 2n * SCALE, 'up')).toBe(1n * SCALE);
    expect(div(1n * SCALE, 3n * SCALE, 'down')).toBe(333_333n);
    expect(div(1n * SCALE, 3n * SCALE, 'up')).toBe(333_334n);
    expect(div(2n * SCALE, 1n * SCALE, 'up')).toBe(2n * SCALE);
  });

  it('no divide por cero: en un libro no hay precio cero', () => {
    expect(() => div(SCALE, 0n, 'down')).toThrow(RangeError);
  });
});

describe('relativeBps', () => {
  it('mide la desviación contra la referencia', () => {
    // 0.50 contra un mid de 0.49 → +204.08bps
    expect(relativeBps(500_000n, 490_000n)).toBe(204);
    // 0.60 contra un mid de 0.61 → -163.93bps
    expect(relativeBps(600_000n, 610_000n)).toBe(-164);
    expect(relativeBps(490_000n, 490_000n)).toBe(0);
  });

  it('conserva el signo: la cola buena informa tanto como la mala', () => {
    expect(relativeBps(495_000n, 500_000n)).toBeLessThan(0);
    expect(relativeBps(505_000n, 500_000n)).toBeGreaterThan(0);
  });

  it('redondea medio lejos del cero, simétricamente', () => {
    // 0.50005 sobre 1.0 → exactamente 0.5bps → 1. Y su reflejo → -1.
    expect(relativeBps(1_000_050n, 1n * SCALE)).toBe(1);
    expect(relativeBps(999_950n, 1n * SCALE)).toBe(-1);
  });

  it('rechaza una referencia que no puede serlo', () => {
    expect(() => relativeBps(500_000n, 0n)).toThrow(RangeError);
  });
});
