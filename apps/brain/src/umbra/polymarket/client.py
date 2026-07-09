"""Cliente async de Polymarket Gamma API.

Sin auth, paginado con cursor offset. Retries con backoff exponencial.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from umbra.logging import get_logger
from umbra.polymarket.schemas import GammaMarket

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

log = get_logger("umbra.polymarket")


class GammaClient:
    def __init__(
        self,
        base_url: str = GAMMA_BASE_URL,
        timeout: float = 15.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "umbraNocti/0.1"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GammaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @retry(
        retry=retry_if_exception_type(
            (httpx.HTTPError, httpx.TransportError, httpx.RemoteProtocolError)
        ),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
        reraise=True,
    )
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def list_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> list[GammaMarket]:
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        data = await self._get("/markets", params=params)
        if not isinstance(data, list):
            log.warning("gamma.unexpected_response", got_type=type(data).__name__)
            return []
        out: list[GammaMarket] = []
        for raw in data:
            try:
                out.append(GammaMarket.model_validate(raw))
            except Exception as exc:
                log.warning(
                    "gamma.parse_failed",
                    market_id=raw.get("id") if isinstance(raw, dict) else None,
                    error=repr(exc),
                )
        return out

    async def iter_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        order: str = "volume24hr",
        page_size: int = 100,
        max_pages: int = 10,
    ):
        offset = 0
        for _ in range(max_pages):
            page = await self.list_markets(
                active=active,
                closed=closed,
                limit=page_size,
                offset=offset,
                order=order,
            )
            if not page:
                return
            for m in page:
                yield m
            if len(page) < page_size:
                return
            offset += page_size

    async def get_markets_by_condition_ids(
        self, condition_ids: list[str], chunk_size: int = 50
    ) -> dict[str, GammaMarket]:
        """Descarga varios mercados en una sola llamada por chunk.

        Gamma acepta el parámetro `condition_ids` repetido (array). Devuelve un
        dict {condition_id: GammaMarket}. Los IDs que no vuelvan simplemente no
        aparecen en el dict — el caller puede hacer fallback individual.
        """
        out: dict[str, GammaMarket] = {}
        for i in range(0, len(condition_ids), chunk_size):
            chunk = condition_ids[i : i + chunk_size]
            data = await self._get(
                "/markets", params={"condition_ids": chunk, "limit": len(chunk)}
            )
            if not isinstance(data, list):
                log.warning("gamma.unexpected_response", got_type=type(data).__name__)
                continue
            for raw in data:
                try:
                    m = GammaMarket.model_validate(raw)
                    out[m.condition_id] = m
                except Exception as exc:
                    log.warning("gamma.parse_failed", error=repr(exc))
        return out

    async def get_market_by_condition_id(self, condition_id: str) -> GammaMarket | None:
        data = await self._get("/markets", params={"condition_ids": condition_id})
        if not isinstance(data, list) or not data:
            return None
        try:
            return GammaMarket.model_validate(data[0])
        except Exception as exc:
            log.warning("gamma.parse_failed", condition_id=condition_id, error=repr(exc))
            return None
