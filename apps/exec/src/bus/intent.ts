/**
 * El contrato de `nocti:intents` y `nocti:fills`, del lado de exec.
 *
 * La fuente de verdad es `packages/contracts/{intent,fill}.schema.json`. Aquí no
 * se importa `ajv`: el validador está escrito a mano, y `intent.test.ts` comprueba
 * que la lista de campos requeridos de este módulo coincide con la del JSON Schema.
 * Si alguien añade un campo requerido al contrato y no lo añade aquí, el test falla.
 * Es menos elegante que un validador generado, pero no mete una dependencia en el
 * camino de una orden.
 *
 * Los mensajes viajan como campos planos del stream, no como un blob JSON: un
 * `XRANGE` desde `redis-cli` tiene que ser legible a las tres de la mañana. Los
 * campos opcionales que valen `null` sencillamente no se escriben.
 *
 * Nada de floats. Todo decimal es un string, de punta a punta.
 */

export const INTENTS_STREAM = 'nocti:intents';
export const FILLS_STREAM = 'nocti:fills';

/** exec consume intents; brain consume fills. Cada uno con su grupo. */
export const EXEC_GROUP = 'exec';
export const BRAIN_GROUP = 'brain';

/** `SET nocti:intent:{intent_id} … NX EX 86400` antes de tocar nada. */
export const INTENT_DEDUP_PREFIX = 'nocti:intent:';
export const INTENT_DEDUP_TTL_SEC = 86_400;

export const HALT_KEY = 'umbra:halt';
export const HALT_REASON_KEY = 'umbra:halt:reason';

export const STRATEGIES = ['overreaction', 'momentum', 'arb', 'diparb', 'smartmoney'] as const;
export const MODES = ['shadow', 'live'] as const;
export const SIDES = ['BUY', 'SELL'] as const;
export const TIFS = ['GTC', 'FOK', 'IOC'] as const;
export const FILL_STATUSES = ['FILLED', 'PARTIAL', 'REJECTED', 'EXPIRED', 'ERROR'] as const;

export type Strategy = (typeof STRATEGIES)[number];
export type Mode = (typeof MODES)[number];
export type Side = (typeof SIDES)[number];
export type Tif = (typeof TIFS)[number];
export type FillStatus = (typeof FILL_STATUSES)[number];

/** Los campos sin los que un intent no es un intent. Espejo de `intent.schema.json:required`. */
export const INTENT_REQUIRED = [
  'intent_id',
  'ts',
  'strategy',
  'mode',
  'condition_id',
  'token_id',
  'side',
  'size_usd',
  'limit_price',
  'tif',
  'max_slippage_bps',
  'expires_at',
] as const;

/** Espejo de `fill.schema.json:required`. */
export const FILL_REQUIRED = [
  'intent_id',
  'ts',
  'mode',
  'status',
  'filled_shares',
  'avg_price',
  'notional_usd',
  'fees_usd',
] as const;

export interface Intent {
  intent_id: string;
  ts: string;
  strategy: Strategy;
  mode: Mode;
  condition_id: string;
  token_id: string;
  side: Side;
  size_usd: string;
  limit_price: string;
  tif: Tif;
  max_slippage_bps: number;
  expires_at: string;
  signal_id: number | null;
  expected_slippage_bps: number | null;
}

export interface Fill {
  intent_id: string;
  ts: string;
  mode: Mode;
  status: FillStatus;
  filled_shares: string;
  avg_price: string;
  notional_usd: string;
  fees_usd: string;
  order_id: string;
  tx_hash: string;
  /** El mid contra el que se midió el slippage. `null` si el libro no tenía dos lados. */
  mid_price: string | null;
  expected_slippage_bps: number | null;
  realized_slippage_bps: number | null;
  error: string;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const CONDITION_ID_RE = /^0x[a-fA-F0-9]{64}$/;
const DECIMAL_RE = /^[0-9]+(\.[0-9]+)?$/;
// Un precio de Polymarket es una probabilidad: [0, 1]. `.5` sin cero delante no vale.
const PRICE_RE = /^0(\.[0-9]+)?$|^1(\.0+)?$/;

export type ParseResult<T> = { ok: true; value: T } | { ok: false; error: string };

function isIsoInstant(raw: string): boolean {
  const t = Date.parse(raw);
  return Number.isFinite(t) && raw.length >= 20;
}

function parseIntegerField(raw: string, name: string, min: number, max: number): ParseResult<number> {
  if (!/^-?[0-9]+$/.test(raw)) return { ok: false, error: `${name} no es un entero: ${raw}` };
  const n = Number(raw);
  if (!Number.isSafeInteger(n)) return { ok: false, error: `${name} fuera de rango: ${raw}` };
  if (n < min || n > max) return { ok: false, error: `${name} fuera de [${min}, ${max}]: ${raw}` };
  return { ok: true, value: n };
}

function parseOptionalInteger(
  fields: Record<string, string>,
  name: string,
): ParseResult<number | null> {
  const raw = fields[name];
  if (raw === undefined || raw === '') return { ok: true, value: null };
  return parseIntegerField(raw, name, Number.MIN_SAFE_INTEGER, Number.MAX_SAFE_INTEGER);
}

/**
 * Campos planos del stream → `Intent`, o el motivo por el que no lo es.
 *
 * Un intent que no valida no se ejecuta. Nunca se «arregla» un campo: si
 * `size_usd` llega raro, el riesgo de adivinar lo que brain quiso decir es
 * exactamente el riesgo que este contrato existe para eliminar.
 */
export function parseIntent(fields: Record<string, string>): ParseResult<Intent> {
  for (const key of INTENT_REQUIRED) {
    if (fields[key] === undefined || fields[key] === '') {
      return { ok: false, error: `falta el campo requerido: ${key}` };
    }
  }

  if (!UUID_RE.test(fields.intent_id)) {
    return { ok: false, error: `intent_id no es un uuid: ${fields.intent_id}` };
  }
  if (!isIsoInstant(fields.ts)) return { ok: false, error: `ts no es ISO-8601: ${fields.ts}` };
  if (!isIsoInstant(fields.expires_at)) {
    return { ok: false, error: `expires_at no es ISO-8601: ${fields.expires_at}` };
  }
  if (!(STRATEGIES as readonly string[]).includes(fields.strategy)) {
    return { ok: false, error: `strategy desconocida: ${fields.strategy}` };
  }
  if (!(MODES as readonly string[]).includes(fields.mode)) {
    return { ok: false, error: `mode desconocido: ${fields.mode}` };
  }
  if (!(SIDES as readonly string[]).includes(fields.side)) {
    return { ok: false, error: `side desconocido: ${fields.side}` };
  }
  if (!(TIFS as readonly string[]).includes(fields.tif)) {
    return { ok: false, error: `tif desconocido: ${fields.tif}` };
  }
  if (!CONDITION_ID_RE.test(fields.condition_id)) {
    return { ok: false, error: `condition_id malformado: ${fields.condition_id}` };
  }
  if (!DECIMAL_RE.test(fields.size_usd)) {
    return { ok: false, error: `size_usd no es un decimal no negativo: ${fields.size_usd}` };
  }
  if (!PRICE_RE.test(fields.limit_price)) {
    return { ok: false, error: `limit_price fuera de [0, 1]: ${fields.limit_price}` };
  }

  const maxSlippage = parseIntegerField(fields.max_slippage_bps, 'max_slippage_bps', 0, 1000);
  if (!maxSlippage.ok) return maxSlippage;

  const signalId = parseOptionalInteger(fields, 'signal_id');
  if (!signalId.ok) return signalId;

  const expected = parseOptionalInteger(fields, 'expected_slippage_bps');
  if (!expected.ok) return expected;

  return {
    ok: true,
    value: {
      intent_id: fields.intent_id,
      ts: fields.ts,
      strategy: fields.strategy as Strategy,
      mode: fields.mode as Mode,
      condition_id: fields.condition_id,
      token_id: fields.token_id,
      side: fields.side as Side,
      size_usd: fields.size_usd,
      limit_price: fields.limit_price,
      tif: fields.tif as Tif,
      max_slippage_bps: maxSlippage.value,
      expires_at: fields.expires_at,
      signal_id: signalId.value,
      expected_slippage_bps: expected.value,
    },
  };
}

/** `Fill` → campos planos. Los `null` no se escriben; brain los lee como ausentes. */
export function encodeFill(fill: Fill): string[] {
  const out: string[] = [];
  const put = (k: string, v: string | number | null): void => {
    if (v === null) return;
    out.push(k, String(v));
  };

  put('intent_id', fill.intent_id);
  put('ts', fill.ts);
  put('mode', fill.mode);
  put('status', fill.status);
  put('filled_shares', fill.filled_shares);
  put('avg_price', fill.avg_price);
  put('notional_usd', fill.notional_usd);
  put('fees_usd', fill.fees_usd);
  put('order_id', fill.order_id);
  put('tx_hash', fill.tx_hash);
  put('mid_price', fill.mid_price);
  put('expected_slippage_bps', fill.expected_slippage_bps);
  put('realized_slippage_bps', fill.realized_slippage_bps);
  put('error', fill.error);
  return out;
}

/** Los pares planos que devuelve `XREADGROUP` → un objeto. */
export function fieldsFromEntry(entry: string[]): Record<string, string> {
  const fields: Record<string, string> = {};
  for (let i = 0; i + 1 < entry.length; i += 2) {
    fields[entry[i]] = entry[i + 1];
  }
  return fields;
}

/** Un fill sin llenado: rechazo, expiración o error. Todo a cero, y el motivo escrito. */
export function emptyFill(
  intent: Pick<Intent, 'intent_id' | 'mode' | 'expected_slippage_bps'>,
  status: Extract<FillStatus, 'REJECTED' | 'EXPIRED' | 'ERROR'>,
  error: string,
  ts: string,
  realizedSlippageBps: number | null = null,
  midPrice: string | null = null,
): Fill {
  return {
    intent_id: intent.intent_id,
    ts,
    mode: intent.mode,
    status,
    filled_shares: '0.000000',
    avg_price: '0.000000',
    notional_usd: '0.000000',
    fees_usd: '0.000000',
    order_id: '',
    tx_hash: '',
    mid_price: midPrice,
    expected_slippage_bps: intent.expected_slippage_bps,
    realized_slippage_bps: realizedSlippageBps,
    error,
  };
}
