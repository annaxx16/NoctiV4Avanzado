/**
 * Proyección del estado de riesgo a los nombres que ya usan el dashboard React
 * y los `displayStatus()` de los dos entrypoints.
 *
 * El guardián es la única verdad. Esto es una vista de solo lectura que se copia
 * sobre el `state` de cada bot antes de pintarlo. Existe para no romper el
 * contrato del dashboard mientras se le quita el estado de debajo.
 */

import {
  currentCapital,
  drawdownFromPeak,
  isPaused,
  isPermanentlyHalted,
  type RiskLimits,
  type RiskState,
} from './state.js';

export interface RiskView {
  dailyPnL: number;
  monthlyPnL: number;
  totalPnL: number;
  consecutiveLosses: number;
  consecutiveWins: number;
  tradesExecuted: number;
  isPaused: boolean;
  pauseUntil: number;
  lastDailyReset: number;
  monthStartTime: number;
  peakCapital: number;
  currentCapital: number;
  currentDrawdown: number;
  permanentlyHalted: boolean;
  smartMoneyTrades: number;
  arbTrades: number;
  dipArbTrades: number;
  directTrades: number;
}

export function riskView(state: RiskState, limits: RiskLimits, nowMs: number): RiskView {
  return {
    dailyPnL: state.dailyPnl,
    monthlyPnL: state.monthlyPnl,
    totalPnL: state.totalPnl,
    consecutiveLosses: state.consecutiveLosses,
    consecutiveWins: state.consecutiveWins,
    tradesExecuted: state.tradesOpened,
    isPaused: isPaused(state, nowMs),
    pauseUntil: state.pauseUntil,
    lastDailyReset: state.lastDailyReset,
    monthStartTime: state.monthStartTime,
    peakCapital: state.peakCapital,
    currentCapital: currentCapital(state, limits),
    currentDrawdown: drawdownFromPeak(state, limits),
    permanentlyHalted: isPermanentlyHalted(state, limits),
    smartMoneyTrades: state.byStrategy.smartMoney,
    arbTrades: state.byStrategy.arbitrage,
    dipArbTrades: state.byStrategy.dipArb,
    directTrades: state.byStrategy.direct,
  };
}
