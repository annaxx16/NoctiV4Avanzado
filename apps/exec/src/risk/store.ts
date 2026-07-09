/**
 * Persistencia del estado de riesgo de exec.
 *
 * Redis, no Postgres. El MERGE_PLAN traía una contradicción: decía "migrar el
 * estado de riesgo de exec a Postgres", pero la Fase 1 estableció que exec habla
 * exactamente dos idiomas —Redis y Polymarket— y no tiene credenciales de la base
 * de datos. Repartirlas entre dos procesos para guardar seis números no vale el
 * precio. Con `appendonly yes`, Redis cumple la única propiedad que hace falta:
 * que el halt permanente sobreviva al proceso.
 *
 * Cuando la Fase 4 le quite a exec el risk engine entero y brain sea la única
 * autoridad de capital, este fichero se borra.
 */

import type Redis from 'ioredis';
import {
  RISK_STATE_KEY,
  decodeRiskState,
  encodeRiskState,
  type RiskLimits,
  type RiskState,
} from './state.js';

export class RiskStore {
  constructor(
    private readonly redis: Redis,
    private readonly key: string = RISK_STATE_KEY,
  ) {}

  /**
   * `null` si nunca se guardó nada. Propaga el error si Redis no responde: quien
   * llama debe distinguir "primer arranque" de "no sé si estoy haltado".
   */
  async load(limits: RiskLimits, nowMs: number): Promise<RiskState | null> {
    const raw = await this.redis.get(this.key);
    if (raw === null) return null;
    return decodeRiskState(raw, limits, nowMs);
  }

  /** Sin TTL. Un halt que caduca solo no es un halt. */
  async save(state: RiskState): Promise<void> {
    await this.redis.set(this.key, encodeRiskState(state));
  }
}
