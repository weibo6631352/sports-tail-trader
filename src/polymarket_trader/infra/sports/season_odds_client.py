"""赛季 outright 隐含概率源（默认 TheOddsAPI 适配器）。

返回 ``SeasonOddsSnapshot``，供 outright 评估器作为反向定价基准。
免费层 500 req/月——配额管理由 worker 通过 TTL 缓存 + per-market 节流控制。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from polymarket_trader.domain.sports_season import SeasonOddsSnapshot
from polymarket_trader.infra.sports.common import (
    normalize_sports_data_error,
    utc_now,
)


class SeasonOddsClient(Protocol):
    """outright 反向定价的标准接口。"""

    async def fetch(self, *, sport_key: str, market_key: str) -> SeasonOddsSnapshot | None: ...

    async def aclose(self) -> None: ...


_DEFAULT_BASE_URL = "https://api.the-odds-api.com"


class TheOddsApiClient:
    """TheOddsAPI v4 outrights 适配器。

    端点形如 ``/v4/sports/{sport_key}/odds?markets=outrights&regions=us``。
    返回的是各 bookmaker 对该 outright market 各个 outcome 的小数赔率；本
    client 用 ``power method`` 去 vig 后求和归一为概率。
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        regions: Sequence[str] = ("us", "eu"),
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._regions = tuple(regions)
        self._now_provider = now_provider
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_s,
            headers={"accept": "application/json"},
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, *, sport_key: str, market_key: str) -> SeasonOddsSnapshot | None:
        if not self._api_key:
            return None
        operation = f"theoddsapi_outrights:{sport_key}"
        try:
            response = await self._client.get(
                f"/v4/sports/{sport_key}/odds",
                params={
                    "apiKey": self._api_key,
                    "regions": ",".join(self._regions),
                    "markets": "outrights",
                    "oddsFormat": "decimal",
                },
            )
            response.raise_for_status()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        try:
            payload = response.json()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        observed_at = utc_now(self._now_provider)
        return parse_theoddsapi_outrights_payload(
            payload,
            market_key=market_key,
            observed_at=observed_at,
        )


def parse_theoddsapi_outrights_payload(
    payload: Any,
    *,
    market_key: str,
    observed_at: datetime | None = None,
) -> SeasonOddsSnapshot | None:
    """TheOddsAPI v4 outrights 响应 → SeasonOddsSnapshot。

    response 顶层是 events 数组，每个 event 含 ``id / sport_key / commence_time
    / bookmakers``；bookmaker 又含 ``markets[]``，市场 key 通常是 "outrights"
    并包含 outcomes 数组（name + price 小数赔率）。返回的概率是跨 bookmaker
    取均值后 power-method de-vig。
    """

    observed_at = observed_at or datetime.now(timezone.utc)
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        return None
    match_key = _normalize_key(market_key)
    matched_event: Mapping[str, Any] | None = None
    for event in payload:
        if not isinstance(event, Mapping):
            continue
        candidates = (
            _normalize_key(str(event.get("id") or "")),
            _normalize_key(str(event.get("sport_key") or "")),
            _normalize_key(_event_label(event)),
        )
        if match_key in candidates:
            matched_event = event
            break
    if matched_event is None:
        return None
    outcome_to_prices: dict[str, list[Decimal]] = {}
    for bookmaker in matched_event.get("bookmakers", ()) or ():
        if not isinstance(bookmaker, Mapping):
            continue
        for market in bookmaker.get("markets", ()) or ():
            if not isinstance(market, Mapping):
                continue
            if str(market.get("key") or "").lower() != "outrights":
                continue
            for outcome in market.get("outcomes", ()) or ():
                if not isinstance(outcome, Mapping):
                    continue
                name = str(outcome.get("name") or "").strip()
                price = _decimal(outcome.get("price"))
                if not name or price is None or price <= 0:
                    continue
                outcome_to_prices.setdefault(name, []).append(price)
    if not outcome_to_prices:
        return None
    raw_probs: dict[str, Decimal] = {}
    for outcome, prices in outcome_to_prices.items():
        mean_decimal = sum(prices) / Decimal(len(prices))
        if mean_decimal <= 0:
            continue
        raw_probs[outcome] = Decimal(1) / mean_decimal
    if not raw_probs:
        return None
    fair = _power_method_devig(dict(raw_probs))
    return SeasonOddsSnapshot(
        market_key=market_key,
        fair_probabilities=fair,
        observed_at=observed_at,
        source="theoddsapi",
        source_event_id=str(matched_event.get("id") or ""),
        source_conflicts=_conflicting_bookmakers(matched_event),
        raw_payload={
            "sport_key": matched_event.get("sport_key"),
            "commence_time": matched_event.get("commence_time"),
        },
    )


def _power_method_devig(raw_probs: Mapping[str, Decimal]) -> Mapping[str, Decimal]:
    """Power method de-vig：透传到 domain/devig.py 共享实现。"""

    from polymarket_trader.domain.devig import devig_implied

    return devig_implied(raw_probs)


def _conflicting_bookmakers(event: Mapping[str, Any]) -> bool:
    bookmakers = event.get("bookmakers") or ()
    return isinstance(bookmakers, Sequence) and len(bookmakers) >= 3


def _event_label(event: Mapping[str, Any]) -> str:
    parts = []
    for key in ("home_team", "away_team", "title"):
        value = event.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts)


def _normalize_key(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


__all__ = ["SeasonOddsClient", "TheOddsApiClient", "parse_theoddsapi_outrights_payload"]
