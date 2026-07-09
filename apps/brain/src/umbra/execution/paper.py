"""Paper Execution Engine.

Dos operaciones:
- OPEN  (execute_signal): el orchestrator pasa una Signal aceptada → fill de apertura,
        slippage adverso al COMPRAR (paga más), suma a la PaperPosition.
- CLOSE (execute_close):  el exit engine pasa una PaperPosition (parcial o total) →
        fill de cierre, slippage adverso al VENDER (recibe menos), resta a la PaperPosition,
        calcula realized PnL y marca la posición closed si shares llegan a 0.

NOTA: en paper no cobramos fees por default (Polymarket actualmente 0% en muchos
mercados). Si tu mercado tiene fees > 0, set fee_bps en config.

DECIMAL, NO FLOAT
-----------------
El camino del dinero —shares, precios, nocional, fees, proceeds, cost basis y PnL
realizado— es `Decimal` de punta a punta. Los `float` solo existen en los bordes:
la configuración de slippage, el `mid_yes` que llega del book, y el `liquidity_usd`
que da Gamma. Se convierten al entrar, con `_dec()`, y no vuelven a salir.

Antes se calculaba todo en float y se envolvía en `Decimal(str(x))` justo al
guardar. Eso no es aritmética decimal: es aritmética binaria con un disfraz al
final. Dos consecuencias concretas, ambas arregladas aquí:

  - `cost_basis_released` se calculaba **dos veces**, una en `execute_close` y otra
    dentro de `_apply_close`, y solo la segunda respetaba el clamp de shares. Cuando
    el clamp mordía, `trade_outcomes` guardaba un cost basis que no cuadraba con el
    `realized_pnl` de su propio fill.

  - `proceeds` entraba a la base sin cuantizar y Postgres lo redondeaba a 6 decimales,
    mientras el PnL realizado se derivaba del valor sin redondear. La identidad
    `realized = notional - fees - cost_basis` no cerraba en la tabla `fills_paper`.

Todo valor se cuantiza a la escala de su columna **antes** de que nada se derive de
él. El redondeo es adverso donde hay una dirección segura: las shares compradas
hacia abajo, las fees hacia arriba, los proceeds hacia abajo. Un backtest no debe
poder halagarse a sí mismo con el sexto decimal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.analytics.trade_outcomes import record_trade_outcome
from umbra.config import settings
from umbra.db.models import PaperFill, PaperPosition, Signal
from umbra.logging import get_logger

log = get_logger("umbra.paper")

# Las escalas de las columnas en db/models.py. Cuantizar a otra cosa es mentirle
# a Postgres, que redondeará por su cuenta y sin avisar.
SHARES = Decimal("0.000001")  # Numeric(20, 6)
PRICE = Decimal("0.000001")  # Numeric(12, 6)
MONEY = Decimal("0.000001")  # Numeric(20, 6)
BPS = Decimal("0.0001")  # Numeric(10, 4)

_PRICE_MIN = Decimal("0.001")
_PRICE_MAX = Decimal("0.999")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS_DENOM = Decimal("10000")

# Por debajo de esto una posición es polvo y se cierra. Es una escala por debajo
# del último decimal que la columna puede guardar.
_DUST = Decimal("0.0000001")


def _dec(x: float | int | Decimal) -> Decimal:
    """Frontera float → Decimal. Vía `str`, nunca `Decimal(float)`.

    `Decimal(0.1)` es 0.1000000000000000055511151231257827; `Decimal(str(0.1))`
    es 0.1. Aquí entra lo que viene del book, de Gamma y de la config.
    """
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _q(x: Decimal, exp: Decimal, rounding: str = ROUND_HALF_UP) -> Decimal:
    return x.quantize(exp, rounding=rounding)


def _clamp(x: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return max(lo, min(hi, x))


def _fee_rate() -> Decimal:
    return _dec(settings.fee_bps) / _BPS_DENOM


# ---------------------------------------------------------------------------
# Slippage
# ---------------------------------------------------------------------------


def _slippage_bps(
    notional_usd: float | Decimal, liquidity_usd: float | Decimal | None
) -> Decimal:
    base = _dec(settings.slippage_base_bps)
    size_factor = _dec(settings.slippage_size_factor_bps)
    cap = _dec(settings.slippage_cap_bps)

    if liquidity_usd is None or _dec(liquidity_usd) <= 0:
        return _q(min(base + size_factor, cap), BPS)

    ratio = abs(_dec(notional_usd)) / _dec(liquidity_usd)
    return _q(min(base + size_factor * ratio, cap), BPS)


def _theoretical_price(side: str, mid_yes: Decimal) -> Decimal:
    if side == "BUY_YES":
        return mid_yes
    if side == "BUY_NO":
        return _ONE - mid_yes
    raise ValueError(f"side desconocido: {side}")


def compute_fill_price(
    side: str,
    mid_yes: float | Decimal,
    notional_usd: float | Decimal,
    liquidity_usd: float | Decimal | None,
) -> tuple[Decimal, Decimal]:
    """Precio de COMPRA (apertura) con slippage adverso AL ALZA.

    Si side=BUY_YES, theoretical = mid_yes; si side=BUY_NO, theoretical = 1 - mid_yes.
    """
    bps = _slippage_bps(notional_usd, liquidity_usd)
    factor = _ONE + (bps / _BPS_DENOM)
    theoretical = _theoretical_price(side, _dec(mid_yes))
    # Se cuantiza antes de recortar: el clamp debe morder sobre el precio que se
    # guardará, no sobre uno que el redondeo aún puede sacar del rango.
    fill_price = _clamp(_q(theoretical * factor, PRICE), _PRICE_MIN, _PRICE_MAX)
    return fill_price, bps


def compute_close_price(
    side: str,
    mid_yes: float | Decimal,
    notional_usd: float | Decimal,
    liquidity_usd: float | Decimal | None,
) -> tuple[Decimal, Decimal]:
    """Precio de VENTA (cierre) con slippage adverso A LA BAJA.

    Recibes menos de lo teórico al cerrar — esto refleja el bid del lado.
    """
    bps = _slippage_bps(notional_usd, liquidity_usd)
    factor = max(_ZERO, _ONE - (bps / _BPS_DENOM))
    theoretical = _theoretical_price(side, _dec(mid_yes))
    close_price = _clamp(_q(theoretical * factor, PRICE), _PRICE_MIN, _PRICE_MAX)
    return close_price, bps


# ---------------------------------------------------------------------------
# Position state mutation
# ---------------------------------------------------------------------------


async def _upsert_open(
    session: AsyncSession,
    market_id: str,
    side: str,
    shares_delta: Decimal,
    cost_delta: Decimal,
    fees_delta: Decimal,
    now: datetime,
) -> None:
    pos = (
        await session.execute(
            select(PaperPosition).where(
                PaperPosition.market_id == market_id,
                PaperPosition.side == side,
            )
        )
    ).scalar_one_or_none()

    def _avg(cost: Decimal, shares: Decimal) -> Decimal:
        return _q(cost / shares, PRICE) if shares > 0 else _ZERO

    if pos is None:
        session.add(
            PaperPosition(
                market_id=market_id,
                side=side,
                opened_at=now,
                last_updated_at=now,
                shares=shares_delta,
                avg_entry_price=_avg(cost_delta, shares_delta),
                total_cost_usd=cost_delta,
                total_fees_usd=fees_delta,
                realized_pnl_usd=_ZERO,
                peak_unrealized_pnl_usd=_ZERO,
                n_fills=1,
                status="open",
            )
        )
        return

    if pos.status == "closed":
        # Reabrir
        pos.status = "open"
        pos.closed_at = None
        pos.opened_at = now
        pos.shares = shares_delta
        pos.total_cost_usd = cost_delta
        pos.total_fees_usd = fees_delta
        pos.avg_entry_price = _avg(cost_delta, shares_delta)
        pos.peak_unrealized_pnl_usd = _ZERO
        pos.n_fills = 1
        pos.last_updated_at = now
        return

    new_shares = pos.shares + shares_delta
    new_cost = pos.total_cost_usd + cost_delta
    pos.shares = new_shares
    pos.total_cost_usd = new_cost
    pos.total_fees_usd = pos.total_fees_usd + fees_delta
    pos.avg_entry_price = _avg(new_cost, new_shares)
    pos.n_fills = pos.n_fills + 1
    pos.last_updated_at = now


def _apply_close(
    pos: PaperPosition,
    shares_closed: Decimal,
    cost_basis_released: Decimal,
    realized: Decimal,
    now: datetime,
) -> None:
    """Aplica al estado de la posición un cierre ya cuantificado.

    No calcula nada. `execute_close` es el único sitio donde se decide cuántas
    shares se cierran, qué cost basis se libera y cuánto PnL se realiza; tenerlo
    aquí también era duplicar la aritmética del dinero en dos lugares que ya
    habían empezado a divergir.
    """
    pos.shares = pos.shares - shares_closed
    pos.total_cost_usd = pos.total_cost_usd - cost_basis_released
    pos.realized_pnl_usd = pos.realized_pnl_usd + realized
    pos.n_fills = pos.n_fills + 1
    pos.last_updated_at = now

    if pos.shares <= _DUST:
        # Cierre total. El coste restante es residuo de cuantizar `avg_entry_price`
        # a 6 decimales; dejarlo colgando de una posición sin shares ensucia
        # `gross_exposure()` en cuanto alguien deje de filtrar por status.
        pos.shares = _ZERO
        pos.total_cost_usd = _ZERO
        pos.status = "closed"
        pos.closed_at = now


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FillResult:
    fill_id: int
    action: str  # 'OPEN' | 'CLOSE'
    side: str
    shares: Decimal  # firmado: + para OPEN, - para CLOSE
    fill_price: Decimal
    notional_usd: Decimal
    slippage_bps: Decimal
    realized_pnl_usd: Decimal


async def execute_signal(
    session: AsyncSession,
    signal: Signal,
    liquidity_usd: float | None,
) -> FillResult | None:
    """OPEN: fill de apertura a partir de una Signal aceptada y persistida."""
    if not signal.accepted or signal.size_shares is None or signal.notional_usd is None:
        return None
    if signal.size_shares <= 0:
        return None

    # `market_price` y `notional_usd` son columnas Numeric, así que ya son Decimal.
    # Se cuantizan igualmente: una Signal recién construida y todavía sin `refresh()`
    # conserva el valor que le pasaron, no el que Postgres guardaría. Todo lo que se
    # derive del nocional debe salir del mismo nocional que acaba en la base.
    mid_yes = _q(signal.market_price, PRICE)
    notional = _q(signal.notional_usd, MONEY)

    fill_price, bps = compute_fill_price(signal.side, mid_yes, notional, liquidity_usd)
    if fill_price <= 0:
        return None

    # Hacia abajo: nunca acreditamos más shares de las que el dinero compró.
    shares = _q(notional / fill_price, SHARES, ROUND_DOWN)
    if shares <= 0:
        return None

    # Hacia arriba: las fees se pagan.
    fees = _q(notional * _fee_rate(), MONEY, ROUND_UP)
    now = datetime.now(UTC)

    fill = PaperFill(
        ts=now,
        signal_id=signal.id,
        market_id=signal.market_id,
        side=signal.side,
        action="OPEN",
        shares=shares,
        mid_at_fill=mid_yes,
        fill_price=fill_price,
        slippage_bps=bps,
        notional_usd=notional,
        fees_usd=fees,
        realized_pnl_usd=_ZERO,
        mode=signal.mode,
    )
    session.add(fill)
    await session.flush()

    await _upsert_open(
        session=session,
        market_id=signal.market_id,
        side=signal.side,
        shares_delta=shares,
        cost_delta=notional + fees,
        fees_delta=fees,
        now=now,
    )

    log.info(
        "paper.open",
        signal_id=signal.id,
        market_id=signal.market_id,
        side=signal.side,
        shares=float(shares),
        fill_price=float(fill_price),
        slippage_bps=float(bps),
        notional=float(notional),
    )
    return FillResult(
        fill_id=fill.id,
        action="OPEN",
        side=signal.side,
        shares=shares,
        fill_price=fill_price,
        notional_usd=notional,
        slippage_bps=bps,
        realized_pnl_usd=_ZERO,
    )


async def execute_close(
    session: AsyncSession,
    position: PaperPosition,
    current_mid_yes: float,
    liquidity_usd: float | None,
    fraction: float = 1.0,
    reason: str = "unspecified",
    mode: str | None = None,
    at_resolution: bool = False,
) -> FillResult | None:
    """CLOSE parcial o total. `fraction` en (0, 1].

    `at_resolution=True` liquida contra un mercado YA RESUELTO: el contrato vale
    exactamente 1.0 o 0.0, no hay book contra el que cruzar, así que NO se aplica
    slippage ni el clamp [0.001, 0.999]. Esto evita el sesgo de subestimar las
    ganancias resueltas (que nunca cobraban el $1 completo) y de no llevar las
    pérdidas a $0.
    """
    if position.shares <= 0 or position.status == "closed":
        return None

    frac = _clamp(_dec(fraction), _ZERO, _ONE)
    if frac <= 0:
        return None

    # El clamp vive aquí y solo aquí. Antes `_apply_close` recortaba las shares por
    # su cuenta, después de que el fill ya se hubiera escrito con las sin recortar.
    shares_to_close = min(_q(position.shares * frac, SHARES), position.shares)
    if shares_to_close <= 0:
        return None

    mid_yes = _dec(current_mid_yes)
    try:
        side_value = _theoretical_price(position.side, mid_yes)
    except ValueError:
        return None

    if at_resolution:
        # Mercado resuelto: cobramos el valor exacto (1.0 o 0.0), sin slippage.
        close_price = _q(_clamp(side_value, _ZERO, _ONE), PRICE)
        bps = _ZERO
    else:
        theoretical_per_share = _clamp(side_value, _PRICE_MIN, _PRICE_MAX)
        notional_theoretical = shares_to_close * theoretical_per_share
        close_price, bps = compute_close_price(
            position.side, mid_yes, notional_theoretical, liquidity_usd
        )

    # Hacia abajo lo que recibimos, hacia arriba lo que pagamos.
    proceeds = _q(shares_to_close * close_price, MONEY, ROUND_DOWN)
    fees = _q(proceeds * _fee_rate(), MONEY, ROUND_UP)
    proceeds_net = proceeds - fees
    cost_basis_released = _q(shares_to_close * position.avg_entry_price, MONEY)

    # Exacta: los tres son múltiplos de 1e-6. Es la identidad que `fills_paper`
    # debe poder reconstruir fila a fila.
    realized = proceeds_net - cost_basis_released

    now = datetime.now(UTC)
    fill = PaperFill(
        ts=now,
        signal_id=None,
        market_id=position.market_id,
        side=position.side,
        action="CLOSE",
        shares=-shares_to_close,  # firmado negativo
        mid_at_fill=_q(mid_yes, PRICE),
        fill_price=close_price,
        slippage_bps=bps,
        notional_usd=proceeds,  # lo que recibimos bruto
        fees_usd=fees,
        realized_pnl_usd=realized,
        mode=mode or "sim",
    )
    session.add(fill)
    await session.flush()

    _apply_close(
        pos=position,
        shares_closed=shares_to_close,
        cost_basis_released=cost_basis_released,
        realized=realized,
        now=now,
    )

    await record_trade_outcome(
        session,
        close_fill=fill,
        position=position,
        cost_basis_released=cost_basis_released,
        realized_pnl=realized,
        exit_reason=reason,
        market_conditions={
            "current_mid_yes": float(mid_yes),
            "liquidity_usd": liquidity_usd,
            "fraction": float(frac),
            "shares_closed": float(shares_to_close),
            "proceeds_gross_usd": float(proceeds),
            "proceeds_net_usd": float(proceeds_net),
            "cost_basis_released_usd": float(cost_basis_released),
            "slippage_bps": float(bps),
            "at_resolution": at_resolution,
        },
    )

    log.info(
        "paper.close",
        market_id=position.market_id,
        side=position.side,
        reason=reason,
        shares=float(shares_to_close),
        close_price=float(close_price),
        slippage_bps=float(bps),
        proceeds=float(proceeds_net),
        realized_pnl=float(realized),
        fraction=float(frac),
    )
    return FillResult(
        fill_id=fill.id,
        action="CLOSE",
        side=position.side,
        shares=-shares_to_close,
        fill_price=close_price,
        notional_usd=proceeds_net,
        slippage_bps=bps,
        realized_pnl_usd=realized,
    )
