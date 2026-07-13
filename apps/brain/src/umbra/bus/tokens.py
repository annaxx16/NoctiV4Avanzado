"""Qué token del CTF corresponde a cada lado. Una sola implementación.

Un mercado binario de Polymarket tiene dos `clob_token_ids`, emparejados
posicionalmente con `outcomes` (`["Yes", "No"]`). Cuál es cuál **no** está
garantizado por el orden: Gamma los devuelve como se los dio el creador del
mercado.

Esto ya se sabía. `universe/scanner.py` lo resolvía para publicar el universo, y
su comentario lo dice mejor que yo: dar por hecho que el YES es `token_ids[0]`
funciona casi siempre, y cuando no, brain vería todos los precios invertidos y
nada fallaría. Un bug silencioso que cuesta dinero.

La Fase 3 necesita la misma respuesta en el camino del dinero, para poner el
`token_id` de un intent. Tenerla escrita dos veces es tenerla mal una vez.

Sin dependencias: ni config, ni base de datos, ni Redis.
"""

from __future__ import annotations

from collections.abc import Sequence

_YES = "yes"
_NO = "no"


def _normalize(outcome: str) -> str:
    return outcome.strip().lower()


def _token_for_outcome(
    outcomes: Sequence[str] | None,
    token_ids: Sequence[str] | None,
    wanted: str,
) -> str | None:
    """El token cuyo `outcome` es `wanted`, o `None` si no se puede afirmar.

    `strict=False` en el zip es deliberado: si las dos listas no tienen el mismo
    largo, el mercado está mal formado y lo que sobra no se puede emparejar con
    nada. Se devuelve lo que sí case, y `None` si no casa nada.
    """
    for outcome, token in zip(outcomes or [], token_ids or [], strict=False):
        if _normalize(outcome) == wanted and token:
            return token
    return None


def yes_token_id(
    outcomes: Sequence[str] | None, token_ids: Sequence[str] | None
) -> str | None:
    """El token del outcome YES. `None` si no hay uno identificable."""
    return _token_for_outcome(outcomes, token_ids, _YES)


def no_token_id(
    outcomes: Sequence[str] | None, token_ids: Sequence[str] | None
) -> str | None:
    """El token del outcome NO. `None` si no hay uno identificable.

    No se deduce como «el otro»: un mercado con outcomes que no son Yes/No —o con
    tres— devolvería un token arbitrario. Se busca el NO por su nombre, igual que
    el YES, o no se devuelve nada.
    """
    return _token_for_outcome(outcomes, token_ids, _NO)


def token_for_side(
    outcomes: Sequence[str] | None,
    token_ids: Sequence[str] | None,
    side: str,
) -> str | None:
    """El token que compra una posición `BUY_YES` / `BUY_NO`.

    Comprar NO es comprar el token NO. No es vender el token YES: son dos libros
    distintos, con dos spreads distintos, y el slippage de uno no es el del otro.
    """
    if side == "BUY_YES":
        return yes_token_id(outcomes, token_ids)
    if side == "BUY_NO":
        return no_token_id(outcomes, token_ids)
    return None
