"""El contrato de `nocti:intents` y `nocti:fills`, del lado de brain.

La fuente de verdad es `packages/contracts/{intent,fill}.schema.json`. Este módulo
es el espejo en Python de `apps/exec/src/bus/intent.ts`, y `tests/test_bus_contract.py`
comprueba que las tres listas de campos requeridos —schema, TypeScript, Python—
coinciden. Si alguien añade un campo requerido al contrato y se olvida de un lado,
el test falla en vez de hacerlo la orden.

Aquí no hay Redis, ni reloj, ni sesión de base de datos. Entra un dict de strings
y sale un mensaje validado, o el motivo por el que no lo es. Todo lo que decide
dinero se puede probar en una tabla.

DOS IDIOMAS
-----------
brain habla de posiciones: `BUY_YES` / `BUY_NO`, `OPEN` / `CLOSE`. El bus habla de
tokens: `BUY` / `SELL` sobre un `token_id`. Comprar NO es comprar el token NO, no
vender el token YES — son cosas distintas con precios distintos, y confundirlas
invierte el signo del slippage sin que nada falle. La traducción vive en
`intents.py`, y solo ahí.

NADA DE FLOATS
--------------
Todo decimal viaja como string, de punta a punta. Un float en el cable es un
redondeo que nadie pidió y que aparece tres capas más abajo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

INTENTS_STREAM = "nocti:intents"
FILLS_STREAM = "nocti:fills"

# exec consume intents; brain consume fills. Cada uno con su grupo.
EXEC_GROUP = "exec"
BRAIN_GROUP = "brain"

# `SET nocti:intent:{intent_id} … NX EX 86400` antes de tocar nada. Lo hace exec;
# brain lo conoce para poder inspeccionarlo desde un script de operación.
INTENT_DEDUP_PREFIX = "nocti:intent:"
INTENT_DEDUP_TTL_SEC = 86_400

HALT_KEY = "umbra:halt"
HALT_REASON_KEY = "umbra:halt:reason"

STRATEGIES = ("overreaction", "momentum", "arb", "diparb", "smartmoney")
MODES = ("shadow", "live")
BUS_SIDES = ("BUY", "SELL")
TIFS = ("GTC", "FOK", "IOC")
FILL_STATUSES = ("FILLED", "PARTIAL", "REJECTED", "EXPIRED", "ERROR")

# Un fill terminal con `status` en este conjunto no movió nada: shares y nocional
# van a cero y `error` explica por qué.
EMPTY_STATUSES = frozenset({"REJECTED", "EXPIRED", "ERROR"})

# Espejo de `intent.schema.json:required` y de `INTENT_REQUIRED` en intent.ts.
INTENT_REQUIRED = (
    "intent_id",
    "ts",
    "strategy",
    "mode",
    "condition_id",
    "token_id",
    "side",
    "size_usd",
    "limit_price",
    "tif",
    "max_slippage_bps",
    "expires_at",
)

# Espejo de `fill.schema.json:required` y de `FILL_REQUIRED` en intent.ts.
FILL_REQUIRED = (
    "intent_id",
    "ts",
    "mode",
    "status",
    "filled_shares",
    "avg_price",
    "notional_usd",
    "fees_usd",
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_CONDITION_ID_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")
_DECIMAL_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
# Un precio de Polymarket es una probabilidad: [0, 1]. `.5` sin cero delante no vale.
_PRICE_RE = re.compile(r"^0(\.[0-9]+)?$|^1(\.0+)?$")
_INT_RE = re.compile(r"^-?[0-9]+$")

# Las escalas de las columnas en `db/models.py`. El cable no las impone; la base sí.
_MONEY = Decimal("0.000001")  # Numeric(20, 6)
_PRICE = Decimal("0.000001")  # Numeric(12, 6)


class ContractError(ValueError):
    """Un mensaje que no cumple el contrato. Nunca se «arregla»: se descarta."""


# ---------------------------------------------------------------------------
# Formateo hacia el cable
# ---------------------------------------------------------------------------


def format_decimal(value: Decimal) -> str:
    """`Decimal` → el string no negativo con 6 decimales que el schema exige.

    No cuantiza a la baja ni al alza por su cuenta: quien llama ya decidió el
    redondeo adverso que le tocaba. Aquí solo se comprueba que lo hizo y que el
    resultado cabe en la columna. Un valor con más de 6 decimales significa que
    alguien se saltó ese paso, y truncarlo en silencio es justamente el bug que
    la Fase 2 pasó una semana persiguiendo.
    """
    if value < 0:
        raise ContractError(f"el cable no admite decimales negativos: {value}")
    if value != value.quantize(_MONEY):
        raise ContractError(f"{value} tiene más de 6 decimales: cuantiza antes de publicar")
    return f"{value:.6f}"


def format_price(value: Decimal) -> str:
    """`Decimal` → un precio en [0, 1] con 6 decimales."""
    if not (Decimal(0) <= value <= Decimal(1)):
        raise ContractError(f"precio fuera de [0, 1]: {value}")
    if value != value.quantize(_PRICE):
        raise ContractError(f"{value} tiene más de 6 decimales: cuantiza antes de publicar")
    text = f"{value:.6f}"
    if not _PRICE_RE.match(text):  # pragma: no cover — defensa del invariante
        raise ContractError(f"precio malformado para el contrato: {text}")
    return text


def strategy_from_edge_name(edge_name: str) -> str:
    """`overreaction_v1` → `overreaction`. Estricto a propósito.

    El enum del bus no lleva versión: la estrategia es la puerta de aprobación de
    capital, y `overreaction_v2` no es una estrategia nueva. Un edge que no está
    en la tabla no produce intent — inventarle un nombre lo metería en el
    presupuesto de otra.
    """
    base = edge_name.rsplit("_v", 1)[0] if "_v" in edge_name else edge_name
    if base not in STRATEGIES:
        raise ContractError(f"edge sin estrategia en el contrato del bus: {edge_name}")
    return base


# ---------------------------------------------------------------------------
# Lectura desde el cable
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FillMessage:
    """El resultado de un intent, tal y como exec lo publicó.

    Se llama `FillMessage` y no `Fill` porque `db.models.Fill` es la fila. El
    mensaje es lo que dijo exec; la fila es lo que brain decidió creerse.
    """

    intent_id: str
    ts: str
    mode: str
    status: str
    filled_shares: Decimal
    avg_price: Decimal
    notional_usd: Decimal
    fees_usd: Decimal
    order_id: str
    tx_hash: str
    mid_price: Decimal | None
    expected_slippage_bps: int | None
    realized_slippage_bps: int | None
    error: str

    @property
    def is_empty(self) -> bool:
        """`True` si el intent murió sin llenar nada."""
        return self.status in EMPTY_STATUSES


def fields_from_entry(entry: dict[str, str] | list[str]) -> dict[str, str]:
    """Los pares planos de `XREADGROUP` → un dict.

    `redis-py` con `decode_responses=True` ya devuelve un dict. La rama de lista
    existe para los tests, que escriben los campos como el stream los guarda.
    """
    if isinstance(entry, dict):
        return entry
    return {entry[i]: entry[i + 1] for i in range(0, len(entry) - 1, 2)}


def _require(fields: dict[str, str], names: tuple[str, ...]) -> None:
    for key in names:
        if not fields.get(key):
            raise ContractError(f"falta el campo requerido: {key}")


def _decimal(fields: dict[str, str], name: str) -> Decimal:
    raw = fields[name]
    if not _DECIMAL_RE.match(raw):
        raise ContractError(f"{name} no es un decimal no negativo: {raw}")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:  # pragma: no cover — el regex ya lo impide
        raise ContractError(f"{name} no es un decimal: {raw}") from exc


def _optional_decimal(fields: dict[str, str], name: str) -> Decimal | None:
    raw = fields.get(name)
    if raw is None or raw == "":
        return None
    return _decimal(fields, name)


def _check_iso_instant(raw: str, name: str) -> None:
    """Espejo de `isIsoInstant` en intent.ts: parseable y con fecha *y* hora.

    El `len >= 20` de allí descarta un `"2026-07-10"` suelto, que `Date.parse`
    aceptaría como medianoche UTC. Un `expires_at` a medianoche es un intent que
    nace caducado, o que vive un día entero: las dos lecturas son un desastre.
    """
    if len(raw) < 20:
        raise ContractError(f"{name} no es un instante ISO-8601: {raw}")
    try:
        datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ContractError(f"{name} no es ISO-8601: {raw}") from exc


def _optional_int(fields: dict[str, str], name: str) -> int | None:
    """Entero con signo. `realized_slippage_bps` es negativo cuando el libro fue favorable."""
    raw = fields.get(name)
    if raw is None or raw == "":
        return None
    if not _INT_RE.match(raw):
        raise ContractError(f"{name} no es un entero: {raw}")
    return int(raw)


def parse_fill(fields: dict[str, str]) -> FillMessage:
    """Campos planos del stream → `FillMessage`, o `ContractError`.

    Un fill que no valida no se escribe en la contabilidad. No se adivina ningún
    campo: si `notional_usd` llega raro, el riesgo de suponer lo que exec quiso
    decir es exactamente el riesgo que este contrato existe para eliminar.
    """
    _require(fields, FILL_REQUIRED)

    intent_id = fields["intent_id"]
    if not _UUID_RE.match(intent_id):
        raise ContractError(f"intent_id no es un uuid: {intent_id}")

    mode = fields["mode"]
    if mode not in MODES:
        raise ContractError(f"mode desconocido: {mode}")

    status = fields["status"]
    if status not in FILL_STATUSES:
        raise ContractError(f"status desconocido: {status}")

    mid_price = _optional_decimal(fields, "mid_price")
    if mid_price is not None and not (Decimal(0) <= mid_price <= Decimal(1)):
        raise ContractError(f"mid_price fuera de [0, 1]: {mid_price}")

    return FillMessage(
        intent_id=intent_id,
        ts=fields["ts"],
        mode=mode,
        status=status,
        filled_shares=_decimal(fields, "filled_shares"),
        avg_price=_decimal(fields, "avg_price"),
        notional_usd=_decimal(fields, "notional_usd"),
        fees_usd=_decimal(fields, "fees_usd"),
        order_id=fields.get("order_id", ""),
        tx_hash=fields.get("tx_hash", ""),
        mid_price=mid_price,
        expected_slippage_bps=_optional_int(fields, "expected_slippage_bps"),
        realized_slippage_bps=_optional_int(fields, "realized_slippage_bps"),
        error=fields.get("error", ""),
    )


def validate_intent_fields(fields: dict[str, str]) -> None:
    """Comprueba lo que exec comprobará, antes de publicarlo.

    Un intent malformado no llega a ser una orden: `exec` lo aparta en
    `nocti:intents:dead` y brain se queda esperando un fill que nunca llega. Es
    más barato descubrirlo aquí, donde todavía hay un stack trace y un `signal_id`
    a mano, que a las tres de la mañana en un stream muerto.
    """
    _require(fields, INTENT_REQUIRED)

    if not _UUID_RE.match(fields["intent_id"]):
        raise ContractError(f"intent_id no es un uuid: {fields['intent_id']}")
    _check_iso_instant(fields["ts"], "ts")
    _check_iso_instant(fields["expires_at"], "expires_at")
    if not _CONDITION_ID_RE.match(fields["condition_id"]):
        raise ContractError(f"condition_id malformado: {fields['condition_id']}")
    if fields["strategy"] not in STRATEGIES:
        raise ContractError(f"strategy desconocida: {fields['strategy']}")
    if fields["mode"] not in MODES:
        raise ContractError(f"mode desconocido: {fields['mode']}")
    if fields["side"] not in BUS_SIDES:
        raise ContractError(f"side desconocido: {fields['side']}")
    if fields["tif"] not in TIFS:
        raise ContractError(f"tif desconocido: {fields['tif']}")
    if not _DECIMAL_RE.match(fields["size_usd"]):
        raise ContractError(f"size_usd no es un decimal no negativo: {fields['size_usd']}")
    if not _PRICE_RE.match(fields["limit_price"]):
        raise ContractError(f"limit_price fuera de [0, 1]: {fields['limit_price']}")

    if not _INT_RE.match(fields["max_slippage_bps"]):
        raise ContractError(f"max_slippage_bps no es un entero: {fields['max_slippage_bps']}")
    if not 0 <= int(fields["max_slippage_bps"]) <= 1000:
        raise ContractError(f"max_slippage_bps fuera de [0, 1000]: {fields['max_slippage_bps']}")
