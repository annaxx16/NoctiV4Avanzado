/**
 * El estado de riesgo de exec, como lógica pura.
 *
 * Antes vivía en un objeto `state` en memoria, duplicado en los dos entrypoints,
 * con dos copias que ya habían divergido. Un restart del proceso lo borraba: el
 * halt permanente del 40% de pérdida —la última línea de defensa— duraba lo que
 * durase el proceso. Aquí no hay ni Redis ni bot; solo funciones. La persistencia
 * vive en `store.ts` y el pegamento en `guard.ts`.
 *
 * Dos invariantes que gobiernan el diseño:
 *
 *   1. **Se persisten los hechos, se derivan los veredictos.** `haltedPermanently`
 *      se guarda, pero al cargar se recalcula contra `totalPnl`. Si una escritura
 *      se rompe a medias y el flag se pierde, el halt sigue en pie porque el PnL
 *      lo dice. El veredicto no depende de que un booleano sobreviva.
 *
 *   2. **Abrir no es cerrar.** Una apertura no tiene PnL realizado y no toca las
 *      rachas. Ver `recordOpen` / `recordClose`.
 */

export const RISK_STATE_KEY = 'nocti:exec:risk_state';
export const RISK_STATE_VERSION = 1;

export type Strategy = 'smartMoney' | 'arbitrage' | 'dipArb' | 'direct' | 'manual';

const STRATEGIES: readonly Strategy[] = [
  'smartMoney',
  'arbitrage',
  'dipArb',
  'direct',
  'manual',
] as const;

const DAY_MS = 24 * 60 * 60 * 1000;

export interface RiskLimits {
  /** Capital base. El denominador de todos los porcentajes. */
  capitalUsd: number;
  dailyMaxLossPct: number;
  monthlyMaxLossPct: number;
  maxDrawdownFromPeak: number;
  totalMaxLossPct: number;
  pauseOnBreachMinutes: number;
}

/** Los hechos. Todo lo demás se deriva de aquí. */
export interface RiskState {
  version: number;
  totalPnl: number;
  dailyPnl: number;
  monthlyPnl: number;
  peakCapital: number;
  consecutiveLosses: number;
  consecutiveWins: number;
  tradesOpened: number;
  tradesClosed: number;
  byStrategy: Record<Strategy, number>;
  lastDailyReset: number;
  monthStartTime: number;
  /** Epoch ms. `0` = sin pausa. Pausado sii `now < pauseUntil`. */
  pauseUntil: number;
  haltedPermanently: boolean;
  haltReason: string;
}

export type DenyReason =
  | 'permanent_halt'
  | 'paused'
  | 'daily_loss_limit'
  | 'monthly_loss_limit'
  | 'max_drawdown'
  | 'total_loss_limit';

export interface Verdict {
  /** El estado tras aplicar rollovers de periodo, pausas y halts. */
  state: RiskState;
  allowed: boolean;
  reason: DenyReason | 'ok';
  /** Qué cambió, para que quien llame lo loguee. Vacío en el caso normal. */
  events: string[];
}

export function initialState(limits: RiskLimits, nowMs: number): RiskState {
  return {
    version: RISK_STATE_VERSION,
    totalPnl: 0,
    dailyPnl: 0,
    monthlyPnl: 0,
    peakCapital: limits.capitalUsd,
    consecutiveLosses: 0,
    consecutiveWins: 0,
    tradesOpened: 0,
    tradesClosed: 0,
    byStrategy: { smartMoney: 0, arbitrage: 0, dipArb: 0, direct: 0, manual: 0 },
    lastDailyReset: nowMs,
    monthStartTime: nowMs,
    pauseUntil: 0,
    haltedPermanently: false,
    haltReason: '',
  };
}

// ---------------------------------------------------------------------------
// Derivadas
// ---------------------------------------------------------------------------

export function currentCapital(state: RiskState, limits: RiskLimits): number {
  return limits.capitalUsd + state.totalPnl;
}

/** En [0, 1]. Un peak no positivo significa que ya no queda nada que perder. */
export function drawdownFromPeak(state: RiskState, limits: RiskLimits): number {
  if (state.peakCapital <= 0) return 1;
  const dd = (state.peakCapital - currentCapital(state, limits)) / state.peakCapital;
  return dd > 0 ? dd : 0;
}

/** El PnL total a partir del cual no se vuelve a operar nunca. Negativo. */
export function totalLossFloor(limits: RiskLimits): number {
  return -Math.abs(limits.capitalUsd * limits.totalMaxLossPct);
}

/**
 * El halt permanente, derivado. `haltedPermanently` es una caché de esto.
 *
 * Se consulta el PnL además del flag para que un halt no pueda perderse por una
 * escritura a medias: mientras el PnL persistido esté por debajo del suelo, da
 * igual lo que diga el booleano.
 */
export function isPermanentlyHalted(state: RiskState, limits: RiskLimits): boolean {
  return state.haltedPermanently || state.totalPnl <= totalLossFloor(limits);
}

export function isPaused(state: RiskState, nowMs: number): boolean {
  return state.pauseUntil > nowMs;
}

// ---------------------------------------------------------------------------
// Rollover de ventanas
// ---------------------------------------------------------------------------

/**
 * Cierra las ventanas diaria y mensual que hayan expirado.
 *
 * Las ventanas son de tiempo transcurrido desde el último reset, no de calendario.
 * Es lo que hacía el código original y persistirlo lo vuelve correcto: antes, cada
 * restart reiniciaba `lastDailyReset` a `Date.now()` y la ventana diaria no se
 * cerraba nunca del todo.
 */
function rollPeriods(state: RiskState, nowMs: number): { state: RiskState; events: string[] } {
  const events: string[] = [];
  let next = state;

  if (nowMs - next.lastDailyReset >= DAY_MS) {
    events.push(`ventana diaria cerrada con PnL $${next.dailyPnl.toFixed(2)}`);
    next = { ...next, dailyPnl: 0, lastDailyReset: nowMs };
  }
  if (nowMs - next.monthStartTime >= 30 * DAY_MS) {
    events.push(`ventana mensual cerrada con PnL $${next.monthlyPnl.toFixed(2)}`);
    next = { ...next, monthlyPnl: 0, monthStartTime: nowMs };
  }
  return { state: next, events };
}

function trackPeak(state: RiskState, limits: RiskLimits): RiskState {
  const capital = currentCapital(state, limits);
  return capital > state.peakCapital ? { ...state, peakCapital: capital } : state;
}

// ---------------------------------------------------------------------------
// La compuerta
// ---------------------------------------------------------------------------

/**
 * Las cuatro capas, en orden. Pura: no muta `state`, devuelve el nuevo.
 *
 * Capa 1 diaria y capas 2/3 pausan; la capa 4 halta para siempre.
 */
export function evaluate(state: RiskState, limits: RiskLimits, nowMs: number): Verdict {
  if (isPermanentlyHalted(state, limits)) {
    // Si el flag se había perdido pero el PnL lo delata, se reescribe.
    const repaired = state.haltedPermanently
      ? state
      : {
          ...state,
          haltedPermanently: true,
          haltReason: `total_loss_limit ${state.totalPnl.toFixed(2)} <= ${totalLossFloor(limits).toFixed(2)}`,
        };
    return {
      state: repaired,
      allowed: false,
      reason: 'permanent_halt',
      events: repaired === state ? [] : ['halt permanente reconstruido desde el PnL persistido'],
    };
  }

  const rolled = rollPeriods(state, nowMs);
  const events = rolled.events;
  let next = trackPeak(rolled.state, limits);

  if (isPaused(next, nowMs)) {
    return { state: next, allowed: false, reason: 'paused', events };
  }
  if (state.pauseUntil > 0 && !isPaused(next, nowMs)) {
    events.push('pausa cumplida; se reanuda');
    next = { ...next, pauseUntil: 0 };
  }

  // Capa 1 — pérdida diaria
  const dailyFloor = -(limits.capitalUsd * limits.dailyMaxLossPct);
  if (next.dailyPnl <= dailyFloor) {
    next = { ...next, pauseUntil: nowMs + limits.pauseOnBreachMinutes * 60 * 1000 };
    events.push(
      `límite diario roto: $${next.dailyPnl.toFixed(2)} <= $${dailyFloor.toFixed(2)}; ` +
        `pausa de ${limits.pauseOnBreachMinutes} min`,
    );
    return { state: next, allowed: false, reason: 'daily_loss_limit', events };
  }

  // Capa 2 — pérdida mensual
  const monthlyFloor = -(limits.capitalUsd * limits.monthlyMaxLossPct);
  if (next.monthlyPnl <= monthlyFloor) {
    next = { ...next, pauseUntil: nowMs + 30 * DAY_MS };
    events.push(
      `límite mensual roto: $${next.monthlyPnl.toFixed(2)} <= $${monthlyFloor.toFixed(2)}; ` +
        'pausa de 30 días',
    );
    return { state: next, allowed: false, reason: 'monthly_loss_limit', events };
  }

  // Capa 3 — drawdown desde el pico
  const dd = drawdownFromPeak(next, limits);
  if (dd >= limits.maxDrawdownFromPeak) {
    next = { ...next, pauseUntil: nowMs + 7 * DAY_MS };
    events.push(
      `drawdown máximo: ${(dd * 100).toFixed(1)}% >= ${(limits.maxDrawdownFromPeak * 100).toFixed(1)}%; ` +
        `pico $${next.peakCapital.toFixed(2)} → $${currentCapital(next, limits).toFixed(2)}; pausa de 7 días`,
    );
    return { state: next, allowed: false, reason: 'max_drawdown', events };
  }

  // Capa 4 — pérdida total. Sin vuelta atrás.
  // Inalcanzable en la práctica: `isPermanentlyHalted` ya cortó arriba. Se deja
  // por si los límites cambian en caliente y el suelo baja bajo un PnL ya perdido.
  const floor = totalLossFloor(limits);
  if (next.totalPnl <= floor) {
    const reason = `total_loss_limit ${next.totalPnl.toFixed(2)} <= ${floor.toFixed(2)}`;
    next = { ...next, haltedPermanently: true, haltReason: reason };
    events.push(`LÍMITE DE PÉRDIDA TOTAL — trading haltado permanentemente. ${reason}`);
    return { state: next, allowed: false, reason: 'total_loss_limit', events };
  }

  return { state: next, allowed: true, reason: 'ok', events };
}

// ---------------------------------------------------------------------------
// Contabilidad
// ---------------------------------------------------------------------------

/**
 * Se abrió una posición. No hay PnL realizado todavía.
 *
 * Antes esto era `recordTrade(0, strategy)`, y el cero caía en la rama `else` de
 * `if (profit < 0)`: cada apertura ponía `consecutiveLosses` a cero y sumaba una
 * a `consecutiveWins`. Como `consecutiveLosses` es lo que encoge el tamaño de
 * posición en una mala racha, abrir una posición *borraba* la memoria de las
 * pérdidas y el sizing dinámico crecía justo cuando debía encogerse.
 *
 * Una apertura no es una victoria. No toca ni PnL ni rachas.
 */
export function recordOpen(state: RiskState, strategy: Strategy): RiskState {
  return {
    ...state,
    tradesOpened: state.tradesOpened + 1,
    byStrategy: { ...state.byStrategy, [strategy]: state.byStrategy[strategy] + 1 },
  };
}

/**
 * Se cerró una posición con PnL realizado. Aquí sí se mueven las rachas.
 *
 * `pnl === 0` cuenta como no-pérdida (corta una racha de derrotas sin sumar una
 * victoria), que es lo único defendible para un breakeven exacto.
 */
export function recordClose(state: RiskState, pnl: number, limits: RiskLimits): RiskState {
  if (!Number.isFinite(pnl)) throw new TypeError(`PnL no finito: ${pnl}`);

  let next: RiskState = {
    ...state,
    totalPnl: state.totalPnl + pnl,
    dailyPnl: state.dailyPnl + pnl,
    monthlyPnl: state.monthlyPnl + pnl,
    tradesClosed: state.tradesClosed + 1,
    consecutiveLosses: pnl < 0 ? state.consecutiveLosses + 1 : 0,
    consecutiveWins: pnl > 0 ? state.consecutiveWins + 1 : 0,
  };

  next = trackPeak(next, limits);

  // El halt se sella en el mismo paso que lo provoca, para que la escritura que
  // persiste la pérdida persista también el veredicto.
  const floor = totalLossFloor(limits);
  if (!next.haltedPermanently && next.totalPnl <= floor) {
    next = {
      ...next,
      haltedPermanently: true,
      haltReason: `total_loss_limit ${next.totalPnl.toFixed(2)} <= ${floor.toFixed(2)}`,
    };
  }
  return next;
}

/** Una operación que abre y cierra en el mismo acto (arbitraje atómico). */
export function recordRoundTrip(
  state: RiskState,
  pnl: number,
  strategy: Strategy,
  limits: RiskLimits,
): RiskState {
  return recordClose(recordOpen(state, strategy), pnl, limits);
}

/** Halt manual. El operador, o exec al detectar un fallo grave de ejecución. */
export function halt(state: RiskState, reason: string): RiskState {
  return { ...state, haltedPermanently: true, haltReason: reason };
}

// ---------------------------------------------------------------------------
// Serialización
// ---------------------------------------------------------------------------

export function encodeRiskState(state: RiskState): string {
  return JSON.stringify(state);
}

/**
 * Decodifica y **repara**. Un campo ausente o corrupto no debe abrir la compuerta:
 * los contadores dudosos caen a un valor conservador, y `haltedPermanently` se
 * recalcula contra el PnL. Lanza solo si el JSON no es un objeto.
 */
export function decodeRiskState(raw: string, limits: RiskLimits, nowMs: number): RiskState {
  const parsed: unknown = JSON.parse(raw);
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new TypeError('el estado de riesgo persistido no es un objeto');
  }
  const o = parsed as Record<string, unknown>;
  const base = initialState(limits, nowMs);

  const num = (key: keyof RiskState, fallback: number): number => {
    const v = o[key];
    return typeof v === 'number' && Number.isFinite(v) ? v : fallback;
  };

  const byStrategy = { ...base.byStrategy };
  const rawByStrategy = o.byStrategy;
  if (typeof rawByStrategy === 'object' && rawByStrategy !== null) {
    for (const s of STRATEGIES) {
      const v = (rawByStrategy as Record<string, unknown>)[s];
      if (typeof v === 'number' && Number.isFinite(v)) byStrategy[s] = v;
    }
  }

  const state: RiskState = {
    version: RISK_STATE_VERSION,
    totalPnl: num('totalPnl', 0),
    dailyPnl: num('dailyPnl', 0),
    monthlyPnl: num('monthlyPnl', 0),
    // Si el peak persistido se perdió, cae al capital inicial. Es el mínimo honesto:
    // un peak inventado más bajo que el real subestima el drawdown y abre la capa 3.
    peakCapital: Math.max(num('peakCapital', base.peakCapital), limits.capitalUsd + num('totalPnl', 0)),
    consecutiveLosses: num('consecutiveLosses', 0),
    consecutiveWins: num('consecutiveWins', 0),
    tradesOpened: num('tradesOpened', 0),
    tradesClosed: num('tradesClosed', 0),
    byStrategy,
    lastDailyReset: num('lastDailyReset', nowMs),
    monthStartTime: num('monthStartTime', nowMs),
    pauseUntil: num('pauseUntil', 0),
    haltedPermanently: o.haltedPermanently === true,
    haltReason: typeof o.haltReason === 'string' ? o.haltReason : '',
  };

  if (!state.haltedPermanently && state.totalPnl <= totalLossFloor(limits)) {
    state.haltedPermanently = true;
    state.haltReason = `total_loss_limit reconstruido desde PnL ${state.totalPnl.toFixed(2)}`;
  }
  return state;
}
