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
    mode: Literal["sim", "paper", "live"] = Field(default="sim")

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

    # Edge: Overreaction
    overreaction_sigma_threshold: float = Field(default=3.0)
    overreaction_min_snapshots: int = Field(default=10)
    ema_alpha: float = Field(default=0.1)
    enable_momentum_edge: bool = Field(default=True)
    momentum_min_delta: float = Field(default=0.003)
    momentum_lookback_snapshots: int = Field(default=6)

    # Paper execution
    slippage_base_bps: float = Field(default=20.0)
    slippage_size_factor_bps: float = Field(default=200.0)
    slippage_cap_bps: float = Field(default=500.0)
    fee_bps: float = Field(default=0.0)  # Polymarket cobra 0% en la mayoría de mercados hoy

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
