/**
 * Aritmética de punto fijo con 6 decimales, sobre `bigint`.
 *
 * El camino del dinero de `brain` es `Decimal` de punta a punta (ver
 * `execution/paper.py`). Del lado de `exec` no hay `Decimal`: hay `number`, que
 * es un float de 64 bits. `0.1 + 0.2` da `0.30000000000000004`, y caminar un
 * libro es exactamente eso — sumar precios por tamaños, decenas de veces.
 *
 * Se elige `bigint` sobre una dependencia (`decimal.js`) porque el dominio es
 * cerrado y pequeño: precios, shares y dólares, todos a 6 decimales, todos no
 * negativos. Con eso, `floor` y `trunc` coinciden y la aritmética cabe en cuatro
 * funciones.
 *
 * 6 decimales no es una elección: es la escala de las columnas de Postgres donde
 * esto acaba (`Numeric(20,6)`, `Numeric(12,6)`). Cuantizar a otra cosa sería
 * mentirle a la base, que redondearía por su cuenta y sin avisar.
 *
 * El redondeo se pide siempre de forma explícita. No hay `parseFixed(s)` a secas:
 * quien convierte un precio de venta tiene que decir si lo quiere hacia arriba o
 * hacia abajo, porque esa decisión es dinero.
 */

export const SCALE_DECIMALS = 6;
export const SCALE = 10n ** BigInt(SCALE_DECIMALS);

export const ZERO = 0n;
export const BPS_DENOM = 10_000n;

/** Hacia dónde cae lo que no entra en el sexto decimal. */
export type Rounding = 'down' | 'up';

const DECIMAL_RE = /^([0-9]+)(?:\.([0-9]+))?$/;

/**
 * Texto decimal → entero escalado. Solo no negativos: en este módulo no hay
 * cantidades con signo, y aceptar `-` solo serviría para que un bug pasara
 * desapercibido.
 *
 * Acepta `number` porque el orderbook del CLOB llega parseado a float por
 * `market-service`. `String(0.62)` es `"0.62"` — JS imprime el decimal más corto
 * que round-trippea— así que la conversión es exacta para todo lo que tenga
 * menos de 16 dígitos significativos, que es todo lo que hay aquí. El float no
 * sobrevive a esta función: lo que sale es un entero.
 */
export function parseFixed(input: string | number, rounding: Rounding): bigint {
  const raw = typeof input === 'number' ? String(input) : input.trim();

  if (typeof input === 'number' && !Number.isFinite(input)) {
    throw new RangeError(`no es un decimal finito: ${input}`);
  }
  // `String(1e-7)` da `"1e-7"`, y `String(1e21)` da `"1e+21"`. Ninguno de los dos
  // debería aparecer en un libro, pero si aparece hay que gritar, no truncar.
  const m = DECIMAL_RE.exec(raw);
  if (!m) {
    throw new RangeError(`no es un decimal no negativo en notación posicional: ${raw}`);
  }

  const [, intPart, fracPart = ''] = m;
  const kept = fracPart.slice(0, SCALE_DECIMALS).padEnd(SCALE_DECIMALS, '0');
  const dropped = fracPart.slice(SCALE_DECIMALS);

  let value = BigInt(intPart) * SCALE + BigInt(kept);
  // Solo redondea hacia arriba si de verdad se perdió algo distinto de cero.
  if (rounding === 'up' && /[1-9]/.test(dropped)) {
    value += 1n;
  }
  return value;
}

/** Entero escalado → texto decimal con 6 decimales exactos. Lo que viaja por el bus. */
export function formatFixed(value: bigint): string {
  if (value < ZERO) throw new RangeError(`negativo: ${value}`);
  const int = value / SCALE;
  const frac = value % SCALE;
  return `${int}.${frac.toString().padStart(SCALE_DECIMALS, '0')}`;
}

/** a·b, con la escala corregida. `down` trunca; `up` sube si hubo resto. */
export function mul(a: bigint, b: bigint, rounding: Rounding): bigint {
  const product = a * b;
  if (rounding === 'up' && product % SCALE !== ZERO) {
    return product / SCALE + 1n;
  }
  return product / SCALE;
}

/** a/b, con la escala corregida. Lanza si `b` es cero: no hay precio cero en un libro. */
export function div(a: bigint, b: bigint, rounding: Rounding): bigint {
  if (b === ZERO) throw new RangeError('división por cero');
  const numerator = a * SCALE;
  if (rounding === 'up' && numerator % b !== ZERO) {
    return numerator / b + 1n;
  }
  return numerator / b;
}

/**
 * Diferencia relativa contra una referencia, en basis points, redondeada al
 * entero más cercano (medio hacia arriba).
 *
 * Con signo: en `nocti:fills` un slippage negativo significa que el libro te
 * trató mejor que el mid, y esa cola de la distribución es tan informativa como
 * la otra. Aplanarla a cero sesgaría el reporte de la Fase 3 justo en la
 * dirección que lo haría inútil.
 */
export function relativeBps(value: bigint, reference: bigint): number {
  if (reference <= ZERO) throw new RangeError(`referencia no positiva: ${reference}`);
  const delta = value - reference;

  // La división de `bigint` trunca hacia cero, así que el signo se saca fuera y
  // se redondea sobre la magnitud: medio se va lejos del cero, simétricamente.
  const negative = delta < ZERO;
  const magnitude = negative ? -delta : delta;
  const q = (2n * magnitude * BPS_DENOM + reference) / (2n * reference);
  return Number(negative ? -q : q);
}
