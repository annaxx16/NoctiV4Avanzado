"""Fase 1: el book de exec (WebSocket) y su mezcla con el de Gamma (REST).

Todo aquí es lógica pura: ni Redis ni Postgres. Lo que se prueba es el contrato,
que es donde se rompen las fusiones.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from umbra.cache.book_cache import (
    SOURCE_CLOB_WS,
    SOURCE_GAMMA_POLL,
    CachedBook,
    age_seconds,
    decode_book,
)
from umbra.cache.universe_cache import Universe, decode_universe
from umbra.polymarket.schemas import GammaMarket
from umbra.scheduler.poller import build_snapshot, is_usable_ws_book
from umbra.universe.scanner import to_universe_markets, yes_token_id

CID = "0x" + "ab" * 32
NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


def _gamma(**over) -> GammaMarket:
    base = {
        "id": "1",
        "conditionId": CID,
        "question": "¿Sube?",
        "slug": "sube",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "bestBid": 0.40,
        "bestAsk": 0.44,
        "spread": 0.04,
        "lastTradePrice": 0.42,
        "liquidityNum": 12_000.0,
        "volume24hr": 55_000.0,
        "clobTokenIds": ["tok_yes", "tok_no"],
        "outcomes": ["Yes", "No"],
    }
    base.update(over)
    return GammaMarket(**base)


def _ws_book(**over) -> CachedBook:
    base = {
        "condition_id": CID,
        "ts": NOW.isoformat(),
        "best_bid": 0.61,
        "best_ask": 0.62,
        "last_trade_price": 0.615,
        "spread": 0.01,
        "liquidity_num": None,
        "volume_24hr": None,
        "bids": [["0.61", "1200"], ["0.60", "800"]],
        "asks": [["0.62", "950"]],
        "source": SOURCE_CLOB_WS,
    }
    base.update(over)
    return CachedBook(**base)


# --------------------------------------------------------------------------
# Compatibilidad hacia atrás del book
# --------------------------------------------------------------------------

def test_book_escrito_por_el_poller_se_lee_sin_los_campos_nuevos():
    """Un book pre-Fase 1 no tiene bids/asks/source. Debe seguir cargando."""
    viejo = json.dumps(
        {
            "condition_id": CID,
            "ts": NOW.isoformat(),
            "best_bid": 0.40,
            "best_ask": 0.44,
            "last_trade_price": 0.42,
            "spread": 0.04,
            "liquidity_num": 12_000.0,
            "volume_24hr": 55_000.0,
        }
    )
    book = decode_book(viejo)
    assert book.source == SOURCE_GAMMA_POLL
    assert book.bids is None and book.asks is None
    assert book.has_depth is False


def test_book_con_campos_desconocidos_no_tira_al_lector():
    """exec puede correr una versión más nueva del contrato durante un despliegue."""
    futuro = json.dumps({**asdict(_ws_book()), "campo_del_futuro": 42})
    book = decode_book(futuro)
    assert book.has_depth is True
    assert book.best_bid == 0.61


def test_roundtrip_conserva_la_profundidad():
    book = _ws_book()
    assert decode_book(json.dumps(asdict(book))) == book


def test_ts_ilegible_cuenta_como_infinitamente_viejo():
    """Ante la duda, el book está rancio. Es la dirección segura del error."""
    assert age_seconds(_ws_book(ts="no-es-una-fecha")) == float("inf")


# --------------------------------------------------------------------------
# ¿Nos fiamos de este book?
# --------------------------------------------------------------------------

def test_no_nos_fiamos_de_un_book_escrito_por_el_propio_poller():
    """Si no, el poller leería su propia salida y creería que viene del WebSocket."""
    propio = _ws_book(source=SOURCE_GAMMA_POLL)
    assert is_usable_ws_book(propio, now=NOW) is False


def test_no_nos_fiamos_de_un_book_rancio():
    viejo = _ws_book(ts=(NOW - timedelta(seconds=60)).isoformat())
    assert is_usable_ws_book(viejo, now=NOW) is False


def test_no_nos_fiamos_de_un_book_sin_bid_ni_ask():
    vacio = _ws_book(best_bid=None, best_ask=None)
    assert is_usable_ws_book(vacio, now=NOW) is False


def test_un_book_con_solo_un_lado_sigue_valiendo():
    """Un mercado ilíquido puede tener asks y ningún bid. Es información, no ruido."""
    medio = _ws_book(best_bid=None)
    assert is_usable_ws_book(medio, now=NOW) is True


def test_book_ausente_no_vale():
    assert is_usable_ws_book(None, now=NOW) is False


# --------------------------------------------------------------------------
# La mezcla: precios del WebSocket, estado de Gamma
# --------------------------------------------------------------------------

def test_sin_websocket_el_snapshot_es_identico_al_de_antes():
    """El comportamiento pre-Fase 1 se conserva exactamente cuando exec no publica."""
    snap = build_snapshot(CID, _gamma(), None, now=NOW)
    assert float(snap.best_bid) == 0.40
    assert float(snap.best_ask) == 0.44
    assert float(snap.spread) == 0.04
    assert float(snap.last_trade_price) == 0.42


def test_el_websocket_pisa_los_precios_de_gamma():
    snap = build_snapshot(CID, _gamma(), _ws_book(), now=NOW)
    assert float(snap.best_bid) == 0.61
    assert float(snap.best_ask) == 0.62
    assert float(snap.spread) == 0.01


def test_liquidez_y_volumen_siempre_vienen_de_gamma():
    """El WebSocket no los conoce. Si los tomáramos del book, serían None."""
    snap = build_snapshot(CID, _gamma(), _ws_book(), now=NOW)
    assert float(snap.liquidity_num) == 12_000.0
    assert float(snap.volume_24hr) == 55_000.0


def test_el_estado_del_mercado_siempre_viene_de_gamma():
    """Es la razón por la que el poller no se puede apagar: solo Gamma sabe esto."""
    snap = build_snapshot(CID, _gamma(acceptingOrders=False, active=False), _ws_book(), now=NOW)
    assert snap.active is False
    assert snap.accepting_orders is False


def test_si_el_websocket_no_ha_visto_ningun_trade_usa_el_de_gamma():
    """Recién conectado, el WS no conoce el último trade. Gamma es mejor que nada."""
    snap = build_snapshot(CID, _gamma(), _ws_book(last_trade_price=None), now=NOW)
    assert float(snap.last_trade_price) == 0.42


def test_un_book_rancio_no_pisa_a_gamma():
    viejo = _ws_book(ts=(NOW - timedelta(seconds=60)).isoformat())
    snap = build_snapshot(CID, _gamma(), viejo, now=NOW)
    assert float(snap.best_bid) == 0.40


@pytest.mark.parametrize("precio", [0.0, 1.0])
def test_precios_extremos_no_se_confunden_con_ausencia(precio):
    """0.0 es un precio válido en un mercado de predicción, no un `None` disfrazado."""
    snap = build_snapshot(CID, _gamma(), _ws_book(best_bid=precio), now=NOW)
    assert float(snap.best_bid) == precio


# --------------------------------------------------------------------------
# El universo que brain publica para exec
# --------------------------------------------------------------------------

def test_el_universo_lleva_los_token_ids_que_exec_necesita():
    """exec se suscribe al WebSocket por token_id, no por condition_id."""
    markets = to_universe_markets([_gamma()])
    assert len(markets) == 1
    assert markets[0].token_ids == ["tok_yes", "tok_no"]
    assert markets[0].rank == 1


def test_el_token_yes_se_resuelve_contra_outcomes_no_por_posicion():
    """Si Gamma invierte el orden, `token_ids[0]` sería el NO.

    exec publicaría el libro del NO como si fuera el del mercado y brain vería
    todos los precios invertidos, en silencio. Se resuelve aquí, donde conocemos
    `outcomes`.
    """
    invertido = _gamma(outcomes=["No", "Yes"], clobTokenIds=["tok_no", "tok_yes"])
    assert yes_token_id(invertido) == "tok_yes"
    assert to_universe_markets([invertido])[0].yes_token_id == "tok_yes"


def test_el_token_yes_no_distingue_mayusculas():
    assert yes_token_id(_gamma(outcomes=["YES", "NO"])) == "tok_yes"


def test_sin_outcome_yes_no_hay_token_y_exec_no_vigila_el_mercado():
    """Mejor un hueco que un precio invertido."""
    binario_raro = _gamma(outcomes=["Trump", "Biden"])
    assert yes_token_id(binario_raro) is None
    assert to_universe_markets([binario_raro])[0].yes_token_id is None


def test_mas_outcomes_que_tokens_no_revienta():
    assert yes_token_id(_gamma(outcomes=["No", "Yes", "Maybe"], clobTokenIds=["tok_no"])) is None


def test_el_universo_lleva_liquidez_y_volumen_para_que_exec_no_llame_a_gamma():
    markets = to_universe_markets([_gamma()])
    assert markets[0].liquidity_num == 12_000.0
    assert markets[0].volume_24hr == 55_000.0


def test_el_rank_del_universo_respeta_el_orden_de_los_candidatos():
    ranks = [m.rank for m in to_universe_markets([_gamma(id=str(i)) for i in range(3)])]
    assert ranks == [1, 2, 3]


def test_universo_roundtrip():
    universo = Universe(
        ts=NOW.isoformat(),
        markets=to_universe_markets([_gamma()]),
    )
    decodificado = decode_universe(json.dumps(asdict(universo)))
    assert decodificado == universo


def test_universo_con_campos_desconocidos_no_tira_a_exec():
    crudo = json.dumps(
        {
            "ts": NOW.isoformat(),
            "markets": [
                {
                    "condition_id": CID,
                    "rank": 1,
                    "token_ids": ["a", "b"],
                    "yes_token_id": "a",
                    "liquidity_num": 1.0,
                    "volume_24hr": 2.0,
                    "campo_del_futuro": "x",
                }
            ],
        }
    )
    universo = decode_universe(crudo)
    assert universo.markets[0].condition_id == CID
