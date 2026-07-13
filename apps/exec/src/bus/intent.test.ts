import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  FILL_REQUIRED,
  INTENT_REQUIRED,
  MODES,
  SIDES,
  STRATEGIES,
  TIFS,
  FILL_STATUSES,
  encodeFill,
  fieldsFromEntry,
  parseIntent,
  type Fill,
} from './intent.js';

const here = dirname(fileURLToPath(import.meta.url));
const contracts = resolve(here, '../../../../packages/contracts');

function schema(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(resolve(contracts, name), 'utf8'));
}

const CID = '0x' + 'ab'.repeat(32);
const INTENT_ID = '3f2504e0-4f89-41d3-9a0c-0305e82c3301';

function fields(over: Record<string, string> = {}): Record<string, string> {
  return {
    intent_id: INTENT_ID,
    ts: '2026-07-10T12:00:00.000Z',
    strategy: 'overreaction',
    mode: 'shadow',
    condition_id: CID,
    token_id: 'tok_yes',
    side: 'BUY',
    size_usd: '62',
    limit_price: '0.99',
    tif: 'IOC',
    max_slippage_bps: '100',
    expires_at: '2026-07-10T12:01:00.000Z',
    ...over,
  };
}

/**
 * Este bloque es el único CI que tiene el contrato. Si alguien añade un campo
 * requerido a `intent.schema.json` y no lo añade al validador, `exec` lo ignoraría
 * en silencio. Aquí falla.
 */
describe('el validador no se separa del JSON Schema', () => {
  it('los campos requeridos del intent coinciden', () => {
    const required = schema('intent.schema.json').required as string[];
    expect([...INTENT_REQUIRED].sort()).toEqual([...required].sort());
  });

  it('los campos requeridos del fill coinciden', () => {
    const required = schema('fill.schema.json').required as string[];
    expect([...FILL_REQUIRED].sort()).toEqual([...required].sort());
  });

  it('los enums coinciden', () => {
    const props = schema('intent.schema.json').properties as Record<string, { enum?: string[] }>;
    expect(props.strategy.enum).toEqual([...STRATEGIES]);
    expect(props.mode.enum).toEqual([...MODES]);
    expect(props.side.enum).toEqual([...SIDES]);
    expect(props.tif.enum).toEqual([...TIFS]);

    const fillProps = schema('fill.schema.json').properties as Record<string, { enum?: string[] }>;
    expect(fillProps.status.enum).toEqual([...FILL_STATUSES]);
  });

  it('el fill declara expected_slippage_bps, y el intent lo tiene para copiarlo', () => {
    const intentProps = schema('intent.schema.json').properties as Record<string, unknown>;
    const fillProps = schema('fill.schema.json').properties as Record<string, unknown>;
    expect(intentProps.expected_slippage_bps).toBeDefined();
    expect(fillProps.expected_slippage_bps).toBeDefined();
  });
});

describe('parseIntent', () => {
  it('acepta un intent bien formado', () => {
    const r = parseIntent(fields());
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.value.max_slippage_bps).toBe(100);
    expect(r.value.signal_id).toBeNull();
    expect(r.value.expected_slippage_bps).toBeNull();
  });

  it('lee los opcionales cuando vienen', () => {
    const r = parseIntent(fields({ signal_id: '42', expected_slippage_bps: '30' }));
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.value.signal_id).toBe(42);
    expect(r.value.expected_slippage_bps).toBe(30);
  });

  it.each([...INTENT_REQUIRED])('rechaza si falta %s', (key) => {
    const f = fields();
    delete f[key];
    const r = parseIntent(f);
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.error).toContain(key);
  });

  it.each([
    ['intent_id', 'no-soy-un-uuid'],
    ['condition_id', '0xdeadbeef'],
    ['strategy', 'martingala'],
    ['mode', 'paper'],
    ['side', 'HOLD'],
    ['tif', 'AON'],
    ['size_usd', '-5'],
    ['size_usd', 'mucho'],
    ['limit_price', '1.5'],
    ['limit_price', '.5'],
    ['max_slippage_bps', '1001'],
    ['max_slippage_bps', '-1'],
    ['max_slippage_bps', '3.5'],
    ['ts', 'ayer'],
    ['expires_at', 'mañana'],
  ])('rechaza %s = %s', (key, value) => {
    const r = parseIntent(fields({ [key]: value }));
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.error).toContain(key);
  });

  it('nunca arregla un campo: adivinar lo que brain quiso decir es el riesgo', () => {
    // `size_usd` con espacios no se recorta a un número: se rechaza.
    expect(parseIntent(fields({ size_usd: ' 62 ' })).ok).toBe(false);
  });
});

describe('encodeFill', () => {
  const fill: Fill = {
    intent_id: INTENT_ID,
    ts: '2026-07-10T12:00:00.000Z',
    mode: 'shadow',
    status: 'FILLED',
    filled_shares: '100.000000',
    avg_price: '0.620000',
    notional_usd: '62.000000',
    fees_usd: '0.000000',
    order_id: '',
    tx_hash: '',
    mid_price: '0.615000',
    expected_slippage_bps: 30,
    realized_slippage_bps: 81,
    error: '',
  };

  it('round-trippea por los campos planos del stream', () => {
    const decoded = fieldsFromEntry(encodeFill(fill));
    expect(decoded.intent_id).toBe(INTENT_ID);
    expect(decoded.realized_slippage_bps).toBe('81');
    expect(decoded.expected_slippage_bps).toBe('30');
    expect(decoded.status).toBe('FILLED');
  });

  it('los null no se escriben: brain los lee como ausentes', () => {
    const encoded = encodeFill({ ...fill, expected_slippage_bps: null, realized_slippage_bps: null });
    expect(encoded).not.toContain('expected_slippage_bps');
    expect(encoded).not.toContain('realized_slippage_bps');
  });

  it('un cero sí se escribe: no es lo mismo que no saber', () => {
    const encoded = fieldsFromEntry(encodeFill({ ...fill, realized_slippage_bps: 0 }));
    expect(encoded.realized_slippage_bps).toBe('0');
  });
});
