from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://umbra:umbra_dev@localhost:5432/umbra"
    )
    redis_url: str = Field(default="redis://localhost:6379/0")
    log_level: str = Field(default="INFO")
    # `shadow`: paper sigue moviendo las posiciones y, en paralelo, cada señal
    # aceptada emite un intent al bus para que exec lo cotice contra el libro real.
    # No se firma nada. Ver MERGE_PLAN.md §Fase 3.
    mode: Literal["sim", "paper", "live", "shadow"] = Field(default="sim")

    # Admin API: token requerido para /admin/*. Si queda vacío, los endpoints
    # admin se rechazan (fail-closed) — nadie puede flatten/halt sin configurarlo.
    admin_token: str = Field(default="")

    # Polymarket
    polymarket_gamma_url: str = Field(default="https://gamma-api.polymarket.com")

    # Universe scanner
    min_liquidity_usd: float = Field(default=5000.0)
    min_volume_24h_usd: float = Field(default=1000.0)
    universe_top_n: int = Field(default=20)
    universe_scan_interval_sec: int = Field(default=300)
    # Solo mercados Yes/No con los dos tokens identificables. Ver `is_binary_yes_no`
    # y el comentario de `_is_eligible` en universe/scanner.py: los mercados con
    # outcomes con nombre propio no se pueden resolver, y sin resolución no hay
    # outcome, ni Brier, ni calibración, ni salida por acierto.
    #
    # A `False` se recupera el universo entero, con esas consecuencias.
    universe_require_binary_yes_no: bool = Field(default=True)

    # Poller
    poll_interval_sec: int = Field(default=30)

    # Edad máxima de un book del WebSocket (escrito por exec) para fiarnos de sus
    # precios al componer un snapshot. Muy por debajo de poll_interval_sec: si el
    # book es más viejo que un tick del poller, Gamma ya es igual de bueno y
    # además nunca miente sobre el estado del mercado.
    ws_book_max_age_sec: int = Field(default=10)

    # Risk / sizing
    bankroll_usd: float = Field(default=1000.0)
    kelly_kappa: float = Field(default=0.15)
    min_edge: float = Field(default=0.02)
    max_risk_per_trade_usd: float = Field(default=50.0)
    max_exposure_per_market_usd: float = Field(default=200.0)

    # Suelo de nocional — la compuerta 12 del risk engine.
    #
    # Las compuertas 8-11 solo saben recortar, y lo hacen multiplicativamente: un
    # nocional de $6 que atraviesa tres recortes sale convertido en $2. La Fase 3
    # midió lo que cuestan esas órdenes:
    #
    #     <$5    n=181 (52% del flujo)  281 bps de media, y el 100% de los
    #                                   rechazos que emitió exec
    #     $5-10  n= 51                  102 bps
    #     $10-25 n= 54                   57 bps
    #     >=$25  n= 29                   36 bps
    #
    # Un edge en el umbral (`min_edge`=0.02) sobre un contrato a $0,50 son 400 bps
    # brutos. La ida y vuelta del tramo <$5 son 562. Esas órdenes tienen EV
    # negativo por construcción, antes de que el edge se ponga a prueba: no es que
    # el edge sea malo, es que no puede pagar su propio peaje.
    #
    # A 0 el comportamiento es el de siempre.
    min_notional_usd: float = Field(default=10.0, ge=0.0)

    # Edge: Overreaction
    overreaction_sigma_threshold: float = Field(default=3.0)
    overreaction_min_snapshots: int = Field(default=10)
    ema_alpha: float = Field(default=0.1)
    enable_momentum_edge: bool = Field(default=True)
    momentum_min_delta: float = Field(default=0.003)
    momentum_lookback_snapshots: int = Field(default=6)

    # Paper execution
    #
    # El coste de cruzar es, ante todo, el MEDIO SPREAD. Lo midió la Fase 3 sobre
    # 315 fills cotizados contra el libro real: ajustando
    # `predicho = k · medio_spread + size_factor · (nocional/liquidez)` por error
    # absoluto mediano, el óptimo es k=1.0 y size_factor≈0. Sobre el tramo de
    # órdenes pequeñas (n=124) el residuo mediano contra el medio spread es 0.0:
    # pagas el spread, y nada más.
    #
    # El modelo anterior era `base + size_factor · ratio` sin término de spread.
    # Con nocionales de ~$11 contra liquidez de ~$5.000 el segundo término aportaba
    # 0,4 bps, así que en producción devolvía la constante 20,0 en los 345 intents
    # emitidos, sin una sola excepción. Predecía 20 bps donde el libro cobraba 191.
    slippage_spread_factor: float = Field(default=1.0, ge=0.0)
    # Suelo: lo que cuesta cruzar aunque el spread sea cero. Antes era el término
    # base al que se sumaba todo lo demás; ahora es el mínimo del resultado.
    slippage_base_bps: float = Field(default=20.0)
    # El término de impacto sigue aquí y sigue SIN VALIDAR: en la muestra de la
    # Fase 3 los ratios nocional/liquidez van de 1e-5 a 5e-4 y no hay variación
    # suficiente para ajustarlo. Se conserva porque es la única protección si algún
    # día los tamaños crecen; hoy aporta centésimas de punto básico.
    slippage_size_factor_bps: float = Field(default=200.0)
    # Sube de 500 a 1500: con término de spread, un mercado de libro ancho predice
    # por encima de 500 y el tope anterior lo truncaba justo donde el modelo
    # empezaba a acertar. El máximo realizado observado fue 6.932 bps.
    slippage_cap_bps: float = Field(default=1500.0)
    fee_bps: float = Field(default=0.0)  # Polymarket cobra 0% en la mayoría de mercados hoy

    # Bus de intents (Fase 3). Solo se usan cuando `mode == "shadow"`.
    #
    # `intent_max_slippage_bps` es la tolerancia que brain declara: exec llena
    # caminando el libro hasta ese coste y rechaza más allá. No se ata a
    # `slippage_cap_bps` —que es el tope del *modelo* que predice— porque son dos
    # cosas: una es lo que creemos que costará, otra lo que aceptaríamos pagar.
    # Un REJECTED por exceso conserva la medición, así que subir esto no
    # «arregla» nada: solo cambia dónde cae la compuerta.
    intent_max_slippage_bps: int = Field(default=500, ge=0, le=1000)
    # Cuánto vive un intent en el stream. El poller tickea cada 30s; un intent que
    # sobrevive a dos ticks está cotizando contra un libro que ya no existe.
    intent_ttl_sec: int = Field(default=60, ge=1)
    # Cuántos intents sin publicar drena cada barrido del outbox.
    intent_publish_batch: int = Field(default=100, ge=1)

    # Exit engine
    stop_loss_pct: float = Field(default=0.15)  # cierra si pnl_pct <= -15%
    take_profit_pct: float = Field(default=0.25)  # cierra si pnl_pct >= +25%
    trailing_stop_giveback_pct: float = Field(default=0.40)  # si bajamos 40% del peak: cierra
    trailing_arm_pct: float = Field(default=0.10)  # solo arma trailing si peak >= +10%
    position_ttl_hours: float = Field(default=8.0)
    exit_before_resolution_hours: float = Field(default=1.0)
    spread_blowout_multiplier: float = Field(default=3.0)  # spread_now/spread_at_entry > X
    edge_invalidation_sigma: float = Field(default=1.5)  # si el sigma vuelve a cruzar al lado opuesto
    stale_book_max_age_sec: int = Field(default=180)

    # Portfolio caps + drawdown
    max_gross_exposure_pct: float = Field(default=0.50)  # 50% del bankroll
    min_cash_reserve_pct: float = Field(default=0.10)  # nunca <10% en cash
    dd_throttle_pct: float = Field(default=0.10)  # DD > 10%: kappa /= 2
    dd_halt_pct: float = Field(default=0.15)  # DD > 15%: halt + flatten

    # Ventana sobre la que se busca el pico de equity para calcular el drawdown.
    #
    # Antes el pico se medía «desde la última vez que la cartera estuvo plana», con
    # la intención —correcta— de que un pico antiguo no dejara el bot en halt para
    # siempre. Pero el criterio lo marcaba la estrategia, no el reloj: la noche del
    # 28 de julio de 2026 hubo 102 momentos de cartera plana en 24 horas, cada uno
    # reseteando el pico. La equity cayó un 15,82% y la compuerta nunca llegó a ver
    # más de un 14,62%, con el umbral de halt en el 15%. Se libró por 38 centésimas
    # mientras el freno estaba, en la práctica, ciego.
    #
    # Con una ventana temporal la intención original se conserva —el pico caduca—
    # pero quien la hace caducar es el tiempo, no el churn. A 48h, aquella noche
    # habría disparado el halt unas cuatro horas antes.
    dd_peak_window_hours: float = Field(default=48.0, gt=0.0)
    cooldown_minutes: float = Field(default=30.0)  # tras un exit, cooldown por mercado

    # Entry gates de liquidez/spread
    max_spread_for_entry: float = Field(default=0.04)  # 4 céntimos: rechaza arriba
    min_liquidity_for_entry_usd: float = Field(default=3000.0)  # liquidity_num del snapshot
    min_signal_confidence: float = Field(default=0.30)
    max_time_to_resolution_hours_floor: float = Field(default=2.0)  # rechaza si <2h
    redis_fail_closed_in_sim: bool = Field(default=False)

    # Background loops
    exit_loop_interval_sec: int = Field(default=60)
    equity_snapshot_interval_sec: int = Field(default=60)
    outcomes_resolver_interval_sec: int = Field(default=3600)
    ohlc_aggregator_interval_sec: int = Field(default=60)

    # OHLC / TA
    ohlc_intervals: tuple[str, ...] = Field(default=("1m", "5m", "15m", "1h"))
    ohlc_lookback_bars: int = Field(default=120)
    ta_ema_fast: int = Field(default=20)
    ta_ema_slow: int = Field(default=50)
    ta_sr_window: int = Field(default=40)  # bars para buscar swings
    ta_sr_min_touches: int = Field(default=2)
    ta_hard_reject_enabled: bool = Field(default=False)


settings = Settings()
