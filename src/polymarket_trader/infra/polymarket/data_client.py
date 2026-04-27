from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import httpx

from polymarket_trader.infra.polymarket.auth import PolymarketTradingClient
from polymarket_trader.infra.polymarket.base_client import PolymarketRestClientBase
from polymarket_trader.infra.polymarket.schemas import (
    DataPositionDTO,
    normalize_position_payload,
)


def _iter_mappings(payload: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(payload, list):
        return tuple(item for item in payload if isinstance(item, Mapping))
    if isinstance(payload, Mapping):
        for key in ("data", "items", "results", "rows", "positions", "trades"):
            value = payload.get(key)
            if isinstance(value, list):
                return tuple(item for item in value if isinstance(item, Mapping))
        return (payload,)
    return ()


class DataClient(PolymarketRestClientBase):
    """Polymarket Data API 适配器。

    这里只负责把持仓、成交和只读账户查询原始响应转换成内部 DTO，
    避免上层直接依赖 Data API 字段名。
    """

    def __init__(
        self,
        base_url: str = "https://data-api.polymarket.com",
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
        headers: Mapping[str, str] | None = None,
        auth_client: PolymarketTradingClient | None = None,
        positions_path: str = "/positions",
    ) -> None:
        super().__init__(base_url, client=client, timeout_s=timeout_s, headers=headers)
        self._auth_client = auth_client
        self._positions_path = positions_path

    @property
    def has_auth_client(self) -> bool:
        return self._auth_client is not None

    @property
    def default_wallet_address(self) -> str | None:
        if self._auth_client is None:
            return None
        return self._auth_client.get_address()

    @property
    def default_user_address(self) -> str | None:
        return self.default_wallet_address

    def _resolve_user_address(self, user_address: str | None) -> str:
        resolved = user_address or self.default_user_address
        if resolved is None:
            raise ValueError("DataClient requires user_address or an auth client with a default address")
        return resolved

    def _build_market_params(
        self,
        *,
        market_ids: tuple[str, ...] | None = None,
        event_ids: tuple[int, ...] | None = None,
    ) -> dict[str, Any]:
        if market_ids and event_ids:
            raise ValueError("market_ids and event_ids are mutually exclusive")
        params: dict[str, Any] = {}
        if market_ids:
            params["market"] = ",".join(market_ids)
        if event_ids:
            params["eventId"] = ",".join(str(event_id) for event_id in event_ids)
        return params

    async def list_positions(
        self,
        *,
        user_address: str | None = None,
        market_ids: tuple[str, ...] | None = None,
        event_ids: tuple[int, ...] | None = None,
        size_threshold: Decimal | None = None,
        redeemable: bool | None = None,
        mergeable: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
        sort_by: str | None = None,
        sort_direction: str | None = None,
        title: str | None = None,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> tuple[DataPositionDTO, ...]:
        params = {
            "user": self._resolve_user_address(user_address),
            **self._build_market_params(market_ids=market_ids, event_ids=event_ids),
        }
        if size_threshold is not None:
            params["sizeThreshold"] = str(size_threshold)
        if redeemable is not None:
            params["redeemable"] = redeemable
        if mergeable is not None:
            params["mergeable"] = mergeable
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if sort_by is not None:
            params["sortBy"] = sort_by
        if sort_direction is not None:
            params["sortDirection"] = sort_direction
        if title is not None:
            params["title"] = title
        payload = await self.get_json(
            path or self._positions_path,
            params=params,
            headers=self._auth_headers("GET", path or self._positions_path),
            timeout_s=timeout_s,
            operation="data.list_positions",
        )
        return tuple(normalize_position_payload(item) for item in _iter_mappings(payload))

    def _auth_headers(self, method: str, request_path: str) -> Mapping[str, str] | None:
        if self._auth_client is None:
            return None
        return self._auth_client.build_l2_headers(method=method, request_path=request_path)


__all__ = ["DataClient"]
