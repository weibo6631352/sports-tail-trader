from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from polymarket_trader.domain.discovery import RawMarketEvent
from polymarket_trader.infra.polymarket.base_client import PolymarketRestClientBase
from polymarket_trader.infra.polymarket.schemas import (
    GammaEventDTO,
    GammaMarketDTO,
    GammaPublicProfileDTO,
    normalize_gamma_event,
    normalize_gamma_market,
    normalize_gamma_public_profile,
)


def _iter_mappings(payload: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(payload, list):
        return tuple(item for item in payload if isinstance(item, Mapping))
    if isinstance(payload, Mapping):
        for key in ("events", "markets", "items", "results", "rows", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return tuple(item for item in value if isinstance(item, Mapping))
        return (payload,)
    return ()


def _normalize_query_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    if params is None:
        return normalized
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, tuple):
            normalized[key] = list(value)
        elif isinstance(value, set):
            normalized[key] = list(value)
        else:
            normalized[key] = value
    return normalized


class GammaClient(PolymarketRestClientBase):
    """Polymarket Gamma 元数据适配器。

    Gamma 是只读 market / event 元数据源，最重要的是把 raw payload 规范成内部 DTO，
    这样上层分类与发现逻辑就不必再关心字段名变化。
    """

    def __init__(
        self,
        base_url: str = "https://gamma-api.polymarket.com",
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
        headers: Mapping[str, str] | None = None,
        events_path: str = "/events",
        markets_path: str = "/markets",
    ) -> None:
        super().__init__(base_url, client=client, timeout_s=timeout_s, headers=headers)
        self._events_path = events_path
        self._markets_path = markets_path
        self._public_profiles: dict[str, GammaPublicProfileDTO] = {}

    @staticmethod
    def _build_query_params(
        *,
        active: bool | None,
        closed: bool | None,
        tag: str | None,
        slug: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
        }
        if active is not None:
            params["active"] = active
        if closed is not None:
            params["closed"] = closed
        if tag is not None:
            params["tag_slug"] = tag
        if slug is not None:
            params["slug"] = slug
        return params

    async def list_events_by_params(
        self,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> tuple[GammaEventDTO, ...]:
        payload = await self.get_json(
            self._events_path,
            params=_normalize_query_params(params),
            timeout_s=timeout_s,
            operation="gamma.list_events",
        )
        return tuple(normalize_gamma_event(item) for item in _iter_mappings(payload))

    async def list_events_keyset_by_params(
        self,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> tuple[tuple[GammaEventDTO, ...], str | None]:
        payload = await self.get_json(
            f"{self._events_path.rstrip('/')}/keyset",
            params=_normalize_query_params(params),
            timeout_s=timeout_s,
            operation="gamma.list_events_keyset",
            unwrap=False,
        )
        if not isinstance(payload, Mapping):
            raise TypeError("gamma keyset events response is not a mapping")
        events = tuple(normalize_gamma_event(item) for item in _iter_mappings(payload))
        next_cursor = payload.get("next_cursor")
        return events, None if next_cursor is None else str(next_cursor)

    async def list_markets_by_params(
        self,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> tuple[GammaMarketDTO, ...]:
        payload = await self.get_json(
            self._markets_path,
            params=_normalize_query_params(params),
            timeout_s=timeout_s,
            operation="gamma.list_markets",
        )
        return tuple(normalize_gamma_market(item) for item in _iter_mappings(payload))

    async def list_markets_keyset_by_params(
        self,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> tuple[tuple[GammaMarketDTO, ...], str | None]:
        payload = await self.get_json(
            f"{self._markets_path.rstrip('/')}/keyset",
            params=_normalize_query_params(params),
            timeout_s=timeout_s,
            operation="gamma.list_markets_keyset",
            unwrap=False,
        )
        if not isinstance(payload, Mapping):
            raise TypeError("gamma keyset markets response is not a mapping")
        markets = tuple(normalize_gamma_market(item) for item in _iter_mappings(payload))
        next_cursor = payload.get("next_cursor")
        return markets, None if next_cursor is None else str(next_cursor)

    async def list_events(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        tag: str | None = None,
        slug: str | None = None,
        limit: int = 100,
        offset: int = 0,
        timeout_s: float | None = None,
    ) -> tuple[GammaEventDTO, ...]:
        return await self.list_events_by_params(
            self._build_query_params(
                active=active,
                closed=closed,
                tag=tag,
                slug=slug,
                limit=limit,
                offset=offset,
            ),
            timeout_s=timeout_s,
        )

    async def list_markets(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        tag: str | None = None,
        slug: str | None = None,
        limit: int = 100,
        offset: int = 0,
        timeout_s: float | None = None,
    ) -> tuple[GammaMarketDTO, ...]:
        return await self.list_markets_by_params(
            self._build_query_params(
                active=active,
                closed=closed,
                tag=tag,
                slug=slug,
                limit=limit,
                offset=offset,
            ),
            timeout_s=timeout_s,
        )

    async def get_event(
        self,
        event_id: str,
        *,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> GammaEventDTO:
        event_path = path or f"{self._events_path.rstrip('/')}/{event_id}"
        payload = await self.get_json(
            event_path,
            timeout_s=timeout_s,
            operation="gamma.get_event",
        )
        if isinstance(payload, Mapping):
            return normalize_gamma_event(payload)
        raise TypeError("gamma event response is not a mapping")

    async def get_market(
        self,
        market_id: str,
        *,
        timeout_s: float | None = None,
        path: str | None = None,
    ) -> GammaMarketDTO:
        market_path = path or f"{self._markets_path.rstrip('/')}/{market_id}"
        payload = await self.get_json(
            market_path,
            timeout_s=timeout_s,
            operation="gamma.get_market",
        )
        if isinstance(payload, Mapping):
            return normalize_gamma_market(payload)
        raise TypeError("gamma market response is not a mapping")

    async def get_public_profile(
        self,
        address: str,
        *,
        timeout_s: float | None = None,
        path: str = "/public-profile",
    ) -> GammaPublicProfileDTO:
        cache_key = address.strip().lower()
        cached = self._public_profiles.get(cache_key)
        if cached is not None:
            return cached
        payload = await self.get_json(
            path,
            params={"address": address},
            timeout_s=timeout_s,
            operation="gamma.get_public_profile",
        )
        if isinstance(payload, Mapping):
            profile = normalize_gamma_public_profile(payload)
            self._public_profiles[cache_key] = profile
            return profile
        raise TypeError("gamma public profile response is not a mapping")

    async def iter_raw_market_events(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        tag: str | None = None,
        slug: str | None = None,
        limit: int = 100,
        offset: int = 0,
        source: str = "gamma",
        timeout_s: float | None = None,
    ) -> tuple[RawMarketEvent, ...]:
        events = await self.list_events(
            active=active,
            closed=closed,
            tag=tag,
            slug=slug,
            limit=limit,
            offset=offset,
            timeout_s=timeout_s,
        )
        raw_events: list[RawMarketEvent] = []
        for event in events:
            raw_events.extend(event.to_raw_market_events(source=source))
        return tuple(raw_events)

    async def discover_events(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        tag: str | None = None,
        slug: str | None = None,
        limit: int = 100,
        offset: int = 0,
        timeout_s: float | None = None,
    ) -> tuple[RawMarketEvent, ...]:
        return await self.iter_raw_market_events(
            active=active,
            closed=closed,
            tag=tag,
            slug=slug,
            limit=limit,
            offset=offset,
            source="gamma.events",
            timeout_s=timeout_s,
        )


__all__ = ["GammaClient"]
