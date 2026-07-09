"""Modelos Pydantic para responses de Gamma API.

Los campos son los que necesitamos. Gamma devuelve más, los ignoramos.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except json.JSONDecodeError:
            pass
    return []


class GammaMarket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    condition_id: str = Field(alias="conditionId")
    question: str
    slug: str

    active: bool = False
    closed: bool = True
    accepting_orders: bool = Field(default=False, alias="acceptingOrders")
    archived: bool = False

    end_date: datetime | None = Field(default=None, alias="endDate")
    start_date: datetime | None = Field(default=None, alias="startDate")

    clob_token_ids: list[str] = Field(default_factory=list, alias="clobTokenIds")
    outcomes: list[str] = Field(default_factory=list)
    outcome_prices: list[str] = Field(default_factory=list, alias="outcomePrices")

    liquidity_num: float | None = Field(default=None, alias="liquidityNum")
    volume_num: float | None = Field(default=None, alias="volumeNum")
    volume_24hr: float | None = Field(default=None, alias="volume24hr")

    best_bid: float | None = Field(default=None, alias="bestBid")
    best_ask: float | None = Field(default=None, alias="bestAsk")
    last_trade_price: float | None = Field(default=None, alias="lastTradePrice")
    spread: float | None = None

    @field_validator("id", "condition_id", mode="before")
    @classmethod
    def _str(cls, v: Any) -> str:
        return str(v) if v is not None else ""

    @field_validator(
        "liquidity_num",
        "volume_num",
        "volume_24hr",
        "best_bid",
        "best_ask",
        "last_trade_price",
        "spread",
        mode="before",
    )
    @classmethod
    def _num(cls, v: Any) -> float | None:
        return _to_float(v)

    @field_validator("clob_token_ids", "outcomes", "outcome_prices", mode="before")
    @classmethod
    def _list(cls, v: Any) -> list[str]:
        return _to_list(v)
