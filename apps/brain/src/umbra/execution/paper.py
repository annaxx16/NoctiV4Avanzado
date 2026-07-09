"""Paper Execution Engine.

Dos operaciones:
- OPEN  (execute_signal): el orchestrator pasa una Signal aceptada → fill de apertura,
        slippage adverso al COMPRAR (paga más), suma a la PaperPosition.
- CLOSE (execute_close):  el exit engine pasa una PaperPosition (parcial o total) →
        fill de cierre, slippage adverso al VENDER (recibe menos), resta a la PaperPosition,
        calcula realized PnL y marca la posición closed si shares llegan a 0.

NOTA: en paper no cobramos fees por default (Polymarket actualmente 0% en muchos
mercados). Si tu mercado tiene fees > 0, set fee_bps en config.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from umbra.config import settings
from umbra.db.models import PaperFill, PaperPosition, Signal
from umbra.logging import get_logger

log = get_logger("umbra.paper")


@dataclass(frozen=True)
class FillResult:
    fill_id: int
    action: str  # 'OPEN' | 'CLOSE'
    side: str
    shares: float  # firmado: + para OPEN, - para CLOSE
    fill_price: float
    notional_usd: float
    slippage_bps: float
    realized_pnl_usd: float


# ---------------------------------------------------------------------------
# Slippage
# ---------------------------------------------------------------------------


def _slippage_bps(notional_usd: float, liquidity_usd: float | None) -> float:
    base = settings.slippage_base_bps
    if liquidity_usd is None or liquidity_usd <= 0:
        return min(base + settings.slippage_size_factor_bps, settings.slippage_cap_bps)
    ratio = abs(notional_usd) / liquidity_usd
    bps = base + settings.slippage_size_factor_bps * ratio
    return min(bps, settings.slippage_cap_bps)


def compute_fill_price(
    side: str, mid_yes: float, notional_usd: float, liquidity_usd: float | None
) -> tuple[float, float]:
    """Precio de COMPRA (apertura) con slippage adverso AL ALZA.

    Si side=BUY_YES, theoretical = mid_yes; si side=BUY_NO, theoretical = 1 - mid_yes.
    """
    bps = _slippage_bps(notional_usd, liquidity_usd)
    factor = 1 + (bps / 10_000.0)
    if side == "BUY_YES":
        theoretical = mid_yes
    elif side == "BUY_NO":
        theoretical = 1 - mid_yes
    else:
        raise ValueError(f"side desconocido: {side}")
    fill_price = min(0.999, max(0.001, theoretical * factor))
    return fill_price, bps


def compute_close_price(
    side: str, mid_yes: float, notional_usd: float, liquidity_usd: float | None
) -> tuple[float, float]:
    """Precio de VENTA (cierre) con slippage adverso A LA BAJA.

    Recibes menos de lo teórico al cerrar — esto refleja el bid del lado.
    """
    bps = _slippage_bps(notional_usd, liquidity_usd)
    factor = max(0.0, 1 - (bps / 10_000.0))
    if side == "BUY_YES":
        theoretical = mid_yes
    elif side == "BUY_NO":
        theoretical = 1 - mid_yes
    else:
        raise ValueError(f"side desconocido: {side}")
    close_price = min(0.999, max(0.001, theoretical * factor))
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

    if pos is None:
        avg_entry = (cost_delta / shares_delta) if shares_delta > 0 else Decimal("0")
        session.add(
            PaperPosition(
                market_id=market_id,
                side=side,
                opened_at=now,
                last_updated_at=now,
                shares=shares_delta,
                avg_entry_price=avg_entry,
                total_cost_usd=cost_delta,
                total_fees_usd=fees_delta,
                realized_pnl_usd=Decimal("0"),
                peak_unrealized_pnl_usd=Decimal("0"),
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
        pos.avg_entry_price = (
            (cost_delta / shares_delta) if shares_delta > 0 else Decimal("0")
        )
        pos.peak_unrealized_pnl_usd = Decimal("0")
        pos.n_fills = 1
        pos.last_updated_at = now
        return

    new_shares = pos.shares + shares_delta
    new_cost = pos.total_cost_usd + cost_delta
    new_fees = pos.total_fees_usd + fees_delta
    pos.shares = new_shares
    pos.total_cost_usd = new_cost
    pos.total_fees_usd = new_fees
    pos.avg_entry_price = (
        (new_cost / new_shares) if new_shares > 0 else Decimal("0")
    )
    pos.n_fills = pos.n_fills + 1
    pos.last_updated_at = now


async def _apply_close(
    session: AsyncSession,
    pos: PaperPosition,
    shares_closed: Decimal,
    proceeds_usd: Decimal,
    now: datetime,
) -> Decimal:
    """Aplica un cierre parcial/total. Devuelve realized PnL del cierre.

    Convención: cost_basis liberado = shares_closed * avg_entry_price.
    realized = proceeds_usd - cost_basis_released.
    """
    if shares_closed <= 0 or pos.shares <= 0:
        return Decimal("0")
    if shares_closed > pos.shares:
        shares_closed = pos.shares

    cost_basis_released = shares_closed * pos.avg_entry_price
    realized = proceeds_usd - cost_basis_released

    pos.shares = pos.shares - shares_closed
    pos.total_cost_usd = pos.total_cost_usd - cost_basis_released
    pos.realized_pnl_usd = pos.realized_pnl_usd + realized
    pos.n_fills = pos.n_fills + 1
    pos.last_updated_at = now
    if pos.shares <= Decimal("0.0000001"):
        pos.shares = Decimal("0")
        pos.status = "closed"
        pos.closed_at = now
    return realized


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


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

    mid_yes = float(signal.market_price)
    notional = float(signal.notional_usd)

    fill_price, bps = compute_fill_price(signal.side, mid_yes, notional, liquidity_usd)
    shares = notional / fill_price if fill_price > 0 else 0.0
    if shares <= 0:
        return None

    fees = notional * (settings.fee_bps / 10_000.0)
    now = datetime.now(UTC)

    fill = PaperFill(
        ts=now,
        signal_id=signal.id,
        market_id=signal.market_id,
        side=signal.side,
        action="OPEN",
        shares=Decimal(str(shares)),
        mid_at_fill=Decimal(str(mid_yes)),
        fill_price=Decimal(str(fill_price)),
        slippage_bps=Decimal(str(bps)),
        notional_usd=Decimal(str(notional)),
        fees_usd=Decimal(str(fees)),
        realized_pnl_usd=Decimal("0"),
        mode=signal.mode,
    )
    session.add(fill)
    await session.flush()

    await _upsert_open(
        session=session,
        market_id=signal.market_id,
        side=signal.side,
        shares_delta=Decimal(str(shares)),
        cost_delta=Decimal(str(notional + fees)),
        fees_delta=Decimal(str(fees)),
        now=now,
    )

    log.info(
        "paper.open",
        signal_id=signal.id,
        market_id=signal.market_id,
        side=signal.side,
        shares=shares,
        fill_price=fill_price,
        slippage_bps=bps,
        notional=notional,
    )
    return FillResult(
        fill_id=fill.id,
        action="OPEN",
        side=signal.side,
        shares=shares,
        fill_price=fill_price,
        notional_usd=notional,
        slippage_bps=bps,
        realized_pnl_usd=0.0,
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
    fraction = max(0.0, min(1.0, fraction))
    if fraction <= 0:
        return None

    shares_to_close = float(position.shares) * fraction
    if shares_to_close <= 0:
        return None

    # Precio teórico por share según el lado.
    if position.side == "BUY_YES":
        side_value = current_mid_yes
    elif position.side == "BUY_NO":
        side_value = 1 - current_mid_yes
    else:
        return None

    if at_resolution:
        # Mercado resuelto: cobramos el valor exacto (1.0 o 0.0), sin slippage.
        close_price = max(0.0, min(1.0, side_value))
        bps = 0.0
    else:
        theoretical_per_share = max(0.001, min(0.999, side_value))
        notional_theoretical = shares_to_close * theoretical_per_share
        close_price, bps = compute_close_price(
            position.side, current_mid_yes, notional_theoretical, liquidity_usd
        )
    proceeds = shares_to_close * close_price
    fees = proceeds * (settings.fee_bps / 10_000.0)
    proceeds_net = proceeds - fees

    now = datetime.now(UTC)
    fill = PaperFill(
        ts=now,
        signal_id=None,
        market_id=position.market_id,
        side=position.side,
        action="CLOSE",
        shares=Decimal(str(-shares_to_close)),  # firmado negativo
        mid_at_fill=Decimal(str(current_mid_yes)),
        fill_price=Decimal(str(close_price)),
        slippage_bps=Decimal(str(bps)),
        notional_usd=Decimal(str(proceeds)),  # lo que recibimos bruto
        fees_usd=Decimal(str(fees)),
        realized_pnl_usd=Decimal("0"),  # se rellena tras _apply_close
        mode=mode or "sim",
    )
    session.add(fill)
    await session.flush()

    realized = await _apply_close(
        session=session,
        pos=position,
        shares_closed=Decimal(str(shares_to_close)),
        proceeds_usd=Decimal(str(proceeds_net)),
        now=now,
    )
    fill.realized_pnl_usd = realized

    log.info(
        "paper.close",
        market_id=position.market_id,
        side=position.side,
        reason=reason,
        shares=shares_to_close,
        close_price=close_price,
        slippage_bps=bps,
        proceeds=proceeds_net,
        realized_pnl=float(realized),
        fraction=fraction,
    )
    return FillResult(
        fill_id=fill.id,
        action="CLOSE",
        side=position.side,
        shares=-shares_to_close,
        fill_price=close_price,
        notional_usd=proceeds_net,
        slippage_bps=bps,
        realized_pnl_usd=float(realized),
    )
