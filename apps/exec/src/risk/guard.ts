/**
 * El guardián: estado de riesgo en memoria, respaldado en Redis, con escritura
 * write-through en cada mutación.
 *
 * Es el único objeto que los dos entrypoints (`bot-config.ts`, `bot-with-dashboard.ts`)
 * pueden usar para preguntar "¿puedo operar?" y para anotar lo que pasó. Antes cada
 * uno tenía su propia copia de `canTrade()` y `recordTrade()`, ya divergidas.
 *
 * Fail-closed en tres sitios, por el mismo motivo que `risk/engine.py:66` de brain:
 * un riesgo que no se puede leer se trata como un riesgo activo.
 *
 *   - Si Redis no responde al arrancar, `boot()` lanza. No se opera sin saber
 *     si el halt permanente estaba puesto.
 *   - Si una pérdida no se puede persistir, el guardián queda `degraded` y
 *     `canTrade()` devuelve false hasta que una escritura vuelva a funcionar.
 *     Perder la escritura de una pérdida y seguir operando es cómo se pierde
 *     el rastro de un halt.
 *   - Si el estado persistido está corrupto, `decodeRiskState` lo repara hacia
 *     el lado conservador en vez de empezar de cero.
 */

import {
  evaluate,
  halt,
  initialState,
  isPermanentlyHalted,
  recordClose,
  recordOpen,
  recordRoundTrip,
  type DenyReason,
  type RiskLimits,
  type RiskState,
  type Strategy,
} from './state.js';
import type { RiskStore } from './store.js';

type Logger = {
  info: (msg: string) => void;
  warn: (msg: string) => void;
  error: (msg: string) => void;
};

export interface RiskGuardOptions {
  store: RiskStore;
  limits: RiskLimits;
  logger?: Logger;
  now?: () => number;
  /** Se llama tras cada mutación persistida. Para refrescar el dashboard. */
  onChange?: (state: Readonly<RiskState>) => void;
}

export class RiskGuard {
  private state: RiskState;
  private degraded = false;

  private constructor(
    state: RiskState,
    private readonly store: RiskStore,
    private readonly limits: RiskLimits,
    private readonly log: Logger,
    private readonly now: () => number,
    private readonly onChange: (state: Readonly<RiskState>) => void,
  ) {
    this.state = state;
  }

  /**
   * Carga el estado o lo crea. Lanza si Redis no responde: el proceso no debe
   * arrancar a operar sin saber si estaba haltado.
   */
  static async boot(opts: RiskGuardOptions): Promise<RiskGuard> {
    const log = opts.logger ?? consoleLogger();
    const now = opts.now ?? (() => Date.now());
    const nowMs = now();

    let loaded: RiskState | null;
    try {
      loaded = await opts.store.load(opts.limits, nowMs);
    } catch (err) {
      throw new Error(
        `no pude leer el estado de riesgo de Redis; no arranco sin saber si estoy haltado: ${String(err)}`,
        { cause: err },
      );
    }

    const state = loaded ?? initialState(opts.limits, nowMs);
    const guard = new RiskGuard(
      state,
      opts.store,
      opts.limits,
      log,
      now,
      opts.onChange ?? (() => {}),
    );

    if (loaded === null) {
      log.info(`[risk] sin estado previo; arranco de cero con capital $${opts.limits.capitalUsd.toFixed(2)}`);
      await guard.persist();
    } else {
      log.info(
        `[risk] estado recuperado: PnL total $${state.totalPnl.toFixed(2)}, ` +
          `pico $${state.peakCapital.toFixed(2)}, diario $${state.dailyPnl.toFixed(2)}, ` +
          `${state.tradesOpened} aperturas / ${state.tradesClosed} cierres`,
      );
      if (isPermanentlyHalted(state, opts.limits)) {
        log.error(`[risk] HALT PERMANENTE vigente desde antes de este arranque: ${state.haltReason}`);
      } else if (state.pauseUntil > nowMs) {
        const mins = Math.ceil((state.pauseUntil - nowMs) / 60000);
        log.warn(`[risk] pausa vigente; quedan ${mins} min`);
      }
    }
    return guard;
  }

  get snapshot(): Readonly<RiskState> {
    return this.state;
  }

  get isDegraded(): boolean {
    return this.degraded;
  }

  /** El veredicto de las cuatro capas. Persiste si el estado cambió. */
  async canTrade(): Promise<boolean> {
    if (this.degraded) {
      this.log.error('[risk] estado degradado: no pude persistir una mutación. No opero.');
      return false;
    }

    const verdict = evaluate(this.state, this.limits, this.now());
    for (const e of verdict.events) this.log.warn(`[risk] ${e}`);

    if (verdict.state !== this.state) {
      this.state = verdict.state;
      // Un fallo aquí no cambia el veredicto de esta llamada, pero sí degrada.
      await this.persistOrDegrade();
    }
    if (!verdict.allowed) this.log.warn(`[risk] operación denegada: ${denyLabel(verdict.reason)}`);
    return verdict.allowed;
  }

  /** Se abrió una posición. Sin PnL realizado y sin tocar las rachas. */
  async recordOpen(strategy: Strategy): Promise<void> {
    this.state = recordOpen(this.state, strategy);
    await this.persistOrDegrade();
  }

  /** Se cerró una posición con PnL realizado. */
  async recordClose(pnl: number, strategy: Strategy): Promise<void> {
    const before = this.state.haltedPermanently;
    this.state = recordClose(this.state, pnl, this.limits);
    if (!before && this.state.haltedPermanently) {
      this.log.error(`[risk] LÍMITE DE PÉRDIDA TOTAL. ${this.state.haltReason}`);
      this.log.error('[risk] revisá la estrategia antes de reiniciar con capital nuevo.');
    }
    this.log.info(
      `[risk] cierre ${strategy}: PnL $${pnl.toFixed(2)} → total $${this.state.totalPnl.toFixed(2)}, ` +
        `racha ${this.state.consecutiveLosses}L/${this.state.consecutiveWins}W`,
    );
    await this.persistOrDegrade();
  }

  /** Abre y cierra en el mismo acto: arbitraje atómico. */
  async recordRoundTrip(pnl: number, strategy: Strategy): Promise<void> {
    this.state = recordRoundTrip(this.state, pnl, strategy, this.limits);
    await this.persistOrDegrade();
  }

  /** Halt manual. Se persiste antes de devolver, o lanza. */
  async halt(reason: string): Promise<void> {
    this.state = halt(this.state, reason);
    this.log.error(`[risk] HALT: ${reason}`);
    await this.persist();
    this.degraded = false;
  }

  private async persist(): Promise<void> {
    await this.store.save(this.state);
    this.onChange(this.state);
  }

  /**
   * Persiste; si no puede, degrada en vez de lanzar.
   *
   * Lanzar aquí abortaría el flujo de la estrategia a mitad de una operación ya
   * ejecutada, que es peor. Degradar deja el proceso vivo, cierra la compuerta,
   * y el siguiente `persistOrDegrade` que funcione lo devuelve a la normalidad
   * escribiendo el estado acumulado.
   */
  private async persistOrDegrade(): Promise<void> {
    try {
      await this.persist();
      if (this.degraded) {
        this.degraded = false;
        this.log.info('[risk] Redis responde otra vez; estado persistido y compuerta reabierta');
      }
    } catch (err) {
      this.degraded = true;
      this.log.error(
        `[risk] NO PUDE PERSISTIR el estado de riesgo (${String(err)}). ` +
          'Cierro la compuerta hasta que Redis vuelva.',
      );
    }
  }
}

function denyLabel(reason: DenyReason | 'ok'): string {
  switch (reason) {
    case 'permanent_halt':
      return 'halt permanente — límite de pérdida total alcanzado';
    case 'paused':
      return 'en pausa';
    case 'daily_loss_limit':
      return 'límite de pérdida diaria';
    case 'monthly_loss_limit':
      return 'límite de pérdida mensual';
    case 'max_drawdown':
      return 'drawdown máximo desde el pico';
    case 'total_loss_limit':
      return 'límite de pérdida total';
    case 'ok':
      return 'ok';
  }
}

function consoleLogger(): Logger {
  return {
    info: (m) => console.log(m),
    warn: (m) => console.warn(m),
    error: (m) => console.error(m),
  };
}
