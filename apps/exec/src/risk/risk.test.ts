import { describe, expect, it, vi } from 'vitest';
import type Redis from 'ioredis';
import { RiskGuard } from './guard.js';
import { RiskStore } from './store.js';
import {
  RISK_STATE_KEY,
  decodeRiskState,
  drawdownFromPeak,
  evaluate,
  initialState,
  isPermanentlyHalted,
  recordClose,
  recordOpen,
  totalLossFloor,
  type RiskLimits,
} from './state.js';

const LIMITS: RiskLimits = {
  capitalUsd: 1000,
  dailyMaxLossPct: 0.05, //  -$50
  monthlyMaxLossPct: 0.15, // -$150
  maxDrawdownFromPeak: 0.25,
  totalMaxLossPct: 0.4, //   -$400
  pauseOnBreachMinutes: 60,
};

const T0 = Date.parse('2026-07-08T12:00:00.000Z');
const MIN = 60_000;
const DAY = 24 * 60 * MIN;

/** Redis de mentira: las dos operaciones que el store usa. */
function fakeRedis() {
  const store = new Map<string, string>();
  let failNext = 0;
  return {
    store,
    failNextOps: (n: number) => {
      failNext = n;
    },
    get: vi.fn(async (key: string) => {
      if (failNext > 0) {
        failNext--;
        throw new Error('redis caído');
      }
      return store.get(key) ?? null;
    }),
    set: vi.fn(async (key: string, value: string) => {
      if (failNext > 0) {
        failNext--;
        throw new Error('redis caído');
      }
      store.set(key, value);
      return 'OK';
    }),
  };
}

function silentLogger() {
  return { info: vi.fn(), warn: vi.fn(), error: vi.fn() };
}

async function bootGuard(redis: ReturnType<typeof fakeRedis>, nowMs = T0) {
  let clock = nowMs;
  const guard = await RiskGuard.boot({
    store: new RiskStore(redis as unknown as Redis),
    limits: LIMITS,
    logger: silentLogger(),
    now: () => clock,
  });
  return { guard, tick: (ms: number) => (clock += ms), setClock: (t: number) => (clock = t) };
}

// ---------------------------------------------------------------------------

describe('lógica pura — las cuatro capas', () => {
  it('deja operar cuando no hay nada roto', () => {
    const v = evaluate(initialState(LIMITS, T0), LIMITS, T0);
    expect(v.allowed).toBe(true);
    expect(v.reason).toBe('ok');
  });

  it('capa 1: la pérdida diaria pausa por pauseOnBreachMinutes', () => {
    const s = { ...initialState(LIMITS, T0), dailyPnl: -50 };
    const v = evaluate(s, LIMITS, T0);
    expect(v.allowed).toBe(false);
    expect(v.reason).toBe('daily_loss_limit');
    expect(v.state.pauseUntil).toBe(T0 + 60 * MIN);

    // Dentro de la pausa: denegado por pausa, no por el límite.
    expect(evaluate(v.state, LIMITS, T0 + 30 * MIN).reason).toBe('paused');
    // Pasada la pausa y con la ventana diaria aún abierta, vuelve a pausar.
    expect(evaluate(v.state, LIMITS, T0 + 61 * MIN).reason).toBe('daily_loss_limit');
    // Cerrada la ventana diaria (>24h), el PnL diario se resetea y se reabre.
    const v2 = evaluate(v.state, LIMITS, T0 + DAY + MIN);
    expect(v2.allowed).toBe(true);
    expect(v2.state.dailyPnl).toBe(0);
  });

  it('capa 2: la pérdida mensual pausa 30 días', () => {
    const s = { ...initialState(LIMITS, T0), monthlyPnl: -150, dailyPnl: -10 };
    const v = evaluate(s, LIMITS, T0);
    expect(v.reason).toBe('monthly_loss_limit');
    expect(v.state.pauseUntil).toBe(T0 + 30 * DAY);
  });

  it('capa 3: el drawdown desde el pico pausa 7 días', () => {
    // Subió a 2000 y volvió a 700. DD 65%. El PnL total (-300) sigue por encima
    // del suelo de -400, así que la capa 4 no lo tapa: aísla la capa 3.
    const s = { ...initialState(LIMITS, T0), peakCapital: 2000, totalPnl: -300 };
    expect(drawdownFromPeak(s, LIMITS)).toBeCloseTo(0.65);
    const v = evaluate(s, LIMITS, T0);
    expect(v.reason).toBe('max_drawdown');
    expect(v.state.pauseUntil).toBe(T0 + 7 * DAY);
  });

  it('capa 4: la pérdida total halta para siempre, y el halt gana a todo lo demás', () => {
    expect(totalLossFloor(LIMITS)).toBe(-400);
    const s = recordClose(initialState(LIMITS, T0), -400, LIMITS);
    expect(s.haltedPermanently).toBe(true);

    // Ni el paso del tiempo ni una ganancia posterior lo levantan.
    expect(evaluate(s, LIMITS, T0 + 365 * DAY).reason).toBe('permanent_halt');
    const recovered = recordClose(s, +1000, LIMITS);
    expect(evaluate(recovered, LIMITS, T0 + 365 * DAY).reason).toBe('permanent_halt');
  });

  it('las capas se evalúan en orden: el halt permanente corta antes que la pausa', () => {
    const s = { ...initialState(LIMITS, T0), totalPnl: -500, pauseUntil: T0 + DAY };
    expect(evaluate(s, LIMITS, T0).reason).toBe('permanent_halt');
  });

  it('el pico sube con el capital y no baja', () => {
    let s = initialState(LIMITS, T0);
    s = recordClose(s, +500, LIMITS);
    expect(s.peakCapital).toBe(1500);
    s = recordClose(s, -200, LIMITS);
    expect(s.peakCapital).toBe(1500);
    expect(drawdownFromPeak(s, LIMITS)).toBeCloseTo(200 / 1500);
  });
});

// ---------------------------------------------------------------------------

describe('abrir no es cerrar', () => {
  it('abrir no toca el PnL ni las rachas', () => {
    let s = initialState(LIMITS, T0);
    s = recordClose(s, -10, LIMITS);
    s = recordClose(s, -10, LIMITS);
    expect(s.consecutiveLosses).toBe(2);

    // El bug histórico: `recordTrade(0, 'dipArb')` al abrir caía en la rama
    // `else` de `if (profit < 0)` y contaba la apertura como victoria.
    s = recordOpen(s, 'dipArb');
    expect(s.consecutiveLosses).toBe(2);
    expect(s.consecutiveWins).toBe(0);
    expect(s.totalPnl).toBe(-20);
    expect(s.tradesOpened).toBe(1);
    expect(s.byStrategy.dipArb).toBe(1);
  });

  it('un cierre en breakeven corta la racha de pérdidas sin sumar una victoria', () => {
    let s = recordClose(initialState(LIMITS, T0), -10, LIMITS);
    expect(s.consecutiveLosses).toBe(1);
    s = recordClose(s, 0, LIMITS);
    expect(s.consecutiveLosses).toBe(0);
    expect(s.consecutiveWins).toBe(0);
  });

  it('un cierre con ganancia resetea las pérdidas consecutivas', () => {
    let s = recordClose(initialState(LIMITS, T0), -10, LIMITS);
    s = recordClose(s, +5, LIMITS);
    expect(s.consecutiveLosses).toBe(0);
    expect(s.consecutiveWins).toBe(1);
  });

  it('rechaza un PnL no finito antes de contaminar la contabilidad', () => {
    expect(() => recordClose(initialState(LIMITS, T0), NaN, LIMITS)).toThrow(TypeError);
  });
});

// ---------------------------------------------------------------------------

describe('serialización', () => {
  it('ida y vuelta conserva el estado', () => {
    const s = recordOpen(recordClose(initialState(LIMITS, T0), -33.5, LIMITS), 'arbitrage');
    expect(decodeRiskState(JSON.stringify(s), LIMITS, T0)).toEqual(s);
  });

  it('reconstruye el halt desde el PnL aunque el flag se haya perdido', () => {
    const tampered = JSON.stringify({ ...initialState(LIMITS, T0), totalPnl: -450, haltedPermanently: false });
    const s = decodeRiskState(tampered, LIMITS, T0);
    expect(s.haltedPermanently).toBe(true);
    expect(isPermanentlyHalted(s, LIMITS)).toBe(true);
  });

  it('un estado truncado se repara hacia el lado conservador', () => {
    const s = decodeRiskState('{"totalPnl": -100}', LIMITS, T0);
    expect(s.totalPnl).toBe(-100);
    expect(s.peakCapital).toBe(1000); // el capital inicial, no 900
    expect(drawdownFromPeak(s, LIMITS)).toBeCloseTo(0.1);
    expect(s.consecutiveLosses).toBe(0);
  });

  it('lanza si lo persistido no es un objeto', () => {
    expect(() => decodeRiskState('[1,2,3]', LIMITS, T0)).toThrow(TypeError);
    expect(() => decodeRiskState('"halt"', LIMITS, T0)).toThrow(TypeError);
  });
});

// ---------------------------------------------------------------------------

describe('RiskGuard — persistencia a través del restart', () => {
  it('primer arranque escribe el estado inicial', async () => {
    const redis = fakeRedis();
    await bootGuard(redis);
    expect(redis.store.has(RISK_STATE_KEY)).toBe(true);
  });

  /** El criterio de aceptación de la Fase 2, tal cual está escrito en MERGE_PLAN.md. */
  it('matar el proceso a mitad de sesión conserva peak_capital, daily_pnl y el halt', async () => {
    const redis = fakeRedis();

    // ---- Sesión 1: gana, marca pico, y luego pierde hasta haltar.
    const s1 = await bootGuard(redis);
    await s1.guard.recordClose(+500, 'arbitrage'); // capital 1500, pico 1500
    expect(s1.guard.snapshot.peakCapital).toBe(1500);
    await s1.guard.recordClose(-900, 'dipArb'); // total -400 → suelo alcanzado
    expect(s1.guard.snapshot.haltedPermanently).toBe(true);
    expect(await s1.guard.canTrade()).toBe(false);

    // ---- El proceso muere aquí. No hay shutdown ordenado, no hay flush.

    // ---- Sesión 2: proceso nuevo, mismo Redis.
    const s2 = await bootGuard(redis, T0 + 5 * MIN);
    expect(s2.guard.snapshot.peakCapital).toBe(1500);
    expect(s2.guard.snapshot.totalPnl).toBe(-400);
    expect(s2.guard.snapshot.dailyPnl).toBe(-400);
    expect(s2.guard.snapshot.haltedPermanently).toBe(true);
    expect(await s2.guard.canTrade()).toBe(false);

    // Y sigue haltado un año después, cuando toda pausa habría caducado.
    const s3 = await bootGuard(redis, T0 + 365 * DAY);
    expect(await s3.guard.canTrade()).toBe(false);
  });

  it('una pausa temporal también sobrevive al restart, y caduca sola', async () => {
    const redis = fakeRedis();
    const s1 = await bootGuard(redis);
    await s1.guard.recordClose(-50, 'direct'); // rompe el límite diario
    expect(await s1.guard.canTrade()).toBe(false);
    expect(s1.guard.snapshot.pauseUntil).toBe(T0 + 60 * MIN);

    // Reinicio dentro de la pausa: sigue pausado.
    const s2 = await bootGuard(redis, T0 + 30 * MIN);
    expect(await s2.guard.canTrade()).toBe(false);

    // Reinicio pasada la pausa y pasada la ventana diaria: opera.
    const s3 = await bootGuard(redis, T0 + DAY + 2 * MIN);
    expect(await s3.guard.canTrade()).toBe(true);
  });

  it('el halt manual sobrevive al restart', async () => {
    const redis = fakeRedis();
    const s1 = await bootGuard(redis);
    await s1.guard.halt('fallo grave de ejecución: nonce colisionado');

    const s2 = await bootGuard(redis, T0 + MIN);
    expect(await s2.guard.canTrade()).toBe(false);
    expect(s2.guard.snapshot.haltReason).toContain('nonce colisionado');
  });

  it('los contadores de apertura no se pierden entre sesiones', async () => {
    const redis = fakeRedis();
    const s1 = await bootGuard(redis);
    await s1.guard.recordOpen('smartMoney');
    await s1.guard.recordOpen('dipArb');

    const s2 = await bootGuard(redis, T0 + MIN);
    expect(s2.guard.snapshot.tradesOpened).toBe(2);
    expect(s2.guard.snapshot.byStrategy.smartMoney).toBe(1);
    expect(s2.guard.snapshot.consecutiveWins).toBe(0);
  });
});

// ---------------------------------------------------------------------------

describe('RiskGuard — fail-closed', () => {
  it('no arranca si Redis no responde: no sabe si estaba haltado', async () => {
    const redis = fakeRedis();
    redis.failNextOps(1);
    await expect(bootGuard(redis)).rejects.toThrow(/no arranco sin saber si estoy haltado/);
  });

  it('si no puede persistir una pérdida, cierra la compuerta', async () => {
    const redis = fakeRedis();
    const { guard } = await bootGuard(redis);
    expect(await guard.canTrade()).toBe(true);

    redis.failNextOps(1);
    await guard.recordClose(-10, 'dipArb'); // no lanza: la operación ya ocurrió
    expect(guard.isDegraded).toBe(true);
    expect(await guard.canTrade()).toBe(false);
  });

  it('cuando Redis vuelve, persiste lo acumulado y reabre la compuerta', async () => {
    const redis = fakeRedis();
    const { guard } = await bootGuard(redis);

    redis.failNextOps(1);
    await guard.recordClose(-10, 'dipArb');
    expect(guard.isDegraded).toBe(true);

    await guard.recordClose(-5, 'dipArb'); // esta sí persiste
    expect(guard.isDegraded).toBe(false);
    expect(await guard.canTrade()).toBe(true);

    // Lo que Redis guardó incluye las dos pérdidas, no solo la segunda.
    const s = await bootGuard(redis, T0 + MIN);
    expect(s.guard.snapshot.totalPnl).toBe(-15);
  });

  it('el onChange se dispara en cada mutación persistida', async () => {
    const redis = fakeRedis();
    const onChange = vi.fn();
    const guard = await RiskGuard.boot({
      store: new RiskStore(redis as unknown as Redis),
      limits: LIMITS,
      logger: silentLogger(),
      now: () => T0,
      onChange,
    });
    onChange.mockClear();
    await guard.recordOpen('arbitrage');
    await guard.recordClose(2.5, 'arbitrage');
    expect(onChange).toHaveBeenCalledTimes(2);
  });
});
