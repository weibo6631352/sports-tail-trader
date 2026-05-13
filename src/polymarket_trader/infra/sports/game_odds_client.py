"""单场 h2h（moneyline）赔率源——TheOddsAPI 适配器。

服务 series WINNER 模型的 p_per_game 输入：拉取下一场比赛的 moneyline，
de-vig 后返回 ``GameOddsSnapshot(team_a, team_b, p_a, observed_at, source)``。
team_a/p_a 的 home/away 顺序与 TheOddsAPI event payload 保持一致；调用方
通过 ``single_game_prob._align_p`` 与 SeriesState 顺序匹配。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx

from polymarket_trader.infra.sports.common import (
    normalize_sports_data_error,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class GameOddsSnapshot:
    """单场胜率快照（team_a 在下一场获胜的概率）。"""

    team_a: str
    team_b: str
    p_a: Decimal
    observed_at: datetime
    source: str
    source_event_id: str | None = None
    raw_payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GameSpreadSnapshot:
    """单场让分快照。

    ``spread_line`` 是 ``team_a`` 视角的让分（负数=team_a 让分）。
    ``p_a_covers`` 是 de-vig 后 team_a 在该 line 上覆盖的概率；team_b 的覆盖
    概率是 ``1 - p_a_covers``。
    """

    team_a: str
    team_b: str
    spread_line: Decimal
    p_a_covers: Decimal
    observed_at: datetime
    source: str
    source_event_id: str | None = None
    raw_payload: Mapping[str, Any] = field(default_factory=dict)


class GameOddsClient(Protocol):
    """单场胜率源标准接口。"""

    async def fetch(
        self,
        *,
        sport_key: str,
        game_key: str,
    ) -> GameOddsSnapshot | None: ...

    async def fetch_spreads(
        self,
        *,
        sport_key: str,
        game_key: str,
    ) -> GameSpreadSnapshot | None: ...

    async def aclose(self) -> None: ...


_DEFAULT_BASE_URL = "https://api.the-odds-api.com"


class TheOddsApiGameOddsClient:
    """TheOddsAPI v4 h2h 适配器。

    端点 ``/v4/sports/{sport_key}/odds?markets=h2h&regions=us,eu``。游戏 key
    可以是 event id / "home vs away" 文本——调用方在 game_key_for(market)
    里产出与 TheOddsAPI 命名一致的 key。
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

    async def fetch(
        self,
        *,
        sport_key: str,
        game_key: str,
    ) -> GameOddsSnapshot | None:
        if not self._api_key:
            return None
        operation = f"theoddsapi_h2h:{sport_key}"
        try:
            response = await self._client.get(
                f"/v4/sports/{sport_key}/odds",
                params={
                    "apiKey": self._api_key,
                    "regions": ",".join(self._regions),
                    "markets": "h2h",
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
        return parse_theoddsapi_h2h_payload(
            payload,
            game_key=game_key,
            observed_at=observed_at,
        )

    async def fetch_spreads(
        self,
        *,
        sport_key: str,
        game_key: str,
    ) -> GameSpreadSnapshot | None:
        if not self._api_key:
            return None
        operation = f"theoddsapi_spreads:{sport_key}"
        try:
            response = await self._client.get(
                f"/v4/sports/{sport_key}/odds",
                params={
                    "apiKey": self._api_key,
                    "regions": ",".join(self._regions),
                    "markets": "spreads",
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
        return parse_theoddsapi_spreads_payload(
            payload,
            game_key=game_key,
            observed_at=observed_at,
        )


def parse_theoddsapi_h2h_payload(
    payload: Any,
    *,
    game_key: str,
    observed_at: datetime | None = None,
) -> GameOddsSnapshot | None:
    """TheOddsAPI v4 h2h 响应 → GameOddsSnapshot。

    response 顶层是 events 数组，每个 event 含 ``id / home_team / away_team /
    bookmakers``；bookmaker 又含 ``markets[].key == "h2h"`` 和 outcomes
    （name + price 小数赔率）。home/away 隐含定义在 event 顶层；返回的
    team_a = home_team，p_a = home win probability。
    """

    observed_at = observed_at or datetime.now(timezone.utc)
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        return None
    target = _normalize_key(game_key)
    matched: Mapping[str, Any] | None = None
    for event in payload:
        if not isinstance(event, Mapping):
            continue
        candidates = (
            _normalize_key(str(event.get("id") or "")),
            _normalize_key(_event_label(event)),
        )
        if target in candidates:
            matched = event
            break
    if matched is None:
        return None
    home = str(matched.get("home_team") or "").strip()
    away = str(matched.get("away_team") or "").strip()
    if not home or not away:
        return None
    raw_probs = _aggregate_h2h_probs(matched, home=home, away=away)
    if raw_probs is None:
        return None
    fair = _power_method_devig(raw_probs)
    p_home = fair.get(home)
    if p_home is None or p_home <= 0:
        return None
    return GameOddsSnapshot(
        team_a=home,
        team_b=away,
        p_a=p_home,
        observed_at=observed_at,
        source="theoddsapi",
        source_event_id=str(matched.get("id") or "") or None,
        raw_payload={
            "sport_key": matched.get("sport_key"),
            "commence_time": matched.get("commence_time"),
        },
    )


def _aggregate_h2h_probs(
    event: Mapping[str, Any],
    *,
    home: str,
    away: str,
) -> Mapping[str, Decimal] | None:
    by_outcome: dict[str, list[Decimal]] = {}
    for bookmaker in event.get("bookmakers", ()) or ():
        if not isinstance(bookmaker, Mapping):
            continue
        for market in bookmaker.get("markets", ()) or ():
            if not isinstance(market, Mapping):
                continue
            if str(market.get("key") or "").lower() != "h2h":
                continue
            for outcome in market.get("outcomes", ()) or ():
                if not isinstance(outcome, Mapping):
                    continue
                name = str(outcome.get("name") or "").strip()
                price = _decimal(outcome.get("price"))
                if not name or price is None or price <= 0:
                    continue
                by_outcome.setdefault(name, []).append(price)
    if home not in by_outcome or away not in by_outcome:
        return None
    raw: dict[str, Decimal] = {}
    for name in (home, away):
        prices = by_outcome[name]
        mean_decimal = sum(prices) / Decimal(len(prices))
        if mean_decimal <= 0:
            return None
        raw[name] = Decimal(1) / mean_decimal
    return raw


def _power_method_devig(raw_probs: Mapping[str, Decimal]) -> Mapping[str, Decimal]:
    """h2h 只有两端，de-vig 用比例归一足够；保留 power-method 形式与 outright 对齐。"""

    total = sum(raw_probs.values(), Decimal("0"))
    if total <= 0:
        return {k: Decimal(0) for k in raw_probs}
    return {k: v / total for k, v in raw_probs.items()}


def _event_label(event: Mapping[str, Any]) -> str:
    parts = []
    for key in ("home_team", "away_team"):
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


def parse_theoddsapi_spreads_payload(
    payload: Any,
    *,
    game_key: str,
    observed_at: datetime | None = None,
) -> GameSpreadSnapshot | None:
    """TheOddsAPI v4 spreads 响应 → GameSpreadSnapshot。

    spreads market 每个 outcome 含 ``name`` (球队名)、``price`` (decimal odds) 和
    ``point`` (该球队对应的让分 line)。``team_a = home_team``；``spread_line``
    使用 home 的 point（与 ``p_a_covers`` 同步）。多家 bookmaker 平均 price 后
    用比例归一 de-vig。
    """

    observed_at = observed_at or datetime.now(timezone.utc)
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        return None
    target = _normalize_key(game_key)
    matched: Mapping[str, Any] | None = None
    for event in payload:
        if not isinstance(event, Mapping):
            continue
        candidates = (
            _normalize_key(str(event.get("id") or "")),
            _normalize_key(_event_label(event)),
        )
        if target in candidates:
            matched = event
            break
    if matched is None:
        return None
    home = str(matched.get("home_team") or "").strip()
    away = str(matched.get("away_team") or "").strip()
    if not home or not away:
        return None
    aggregated = _aggregate_spread(matched, home=home, away=away)
    if aggregated is None:
        return None
    home_price, away_price, home_point = aggregated
    raw_probs = {
        home: Decimal(1) / home_price,
        away: Decimal(1) / away_price,
    }
    fair = _power_method_devig(raw_probs)
    p_home = fair.get(home)
    if p_home is None or p_home <= 0:
        return None
    return GameSpreadSnapshot(
        team_a=home,
        team_b=away,
        spread_line=home_point,
        p_a_covers=p_home,
        observed_at=observed_at,
        source="theoddsapi",
        source_event_id=str(matched.get("id") or "") or None,
        raw_payload={
            "sport_key": matched.get("sport_key"),
            "commence_time": matched.get("commence_time"),
        },
    )


def _aggregate_spread(
    event: Mapping[str, Any],
    *,
    home: str,
    away: str,
) -> tuple[Decimal, Decimal, Decimal] | None:
    """从所有 bookmaker 的 spreads market 平均出 (home_price, away_price, home_point)。

    选取规则：每个 bookmaker 内部对 home 与 away 的 ``point`` 必须互为相反数
    （spread 定义本就如此）。多家 bookmaker 的 home_point 可能不同——取
    出现频次最高的 home_point 作为基准；价格在同一 home_point 下平均。
    """

    grouped: dict[Decimal, dict[str, list[Decimal]]] = {}
    for bookmaker in event.get("bookmakers", ()) or ():
        if not isinstance(bookmaker, Mapping):
            continue
        for market in bookmaker.get("markets", ()) or ():
            if not isinstance(market, Mapping):
                continue
            if str(market.get("key") or "").lower() != "spreads":
                continue
            home_price: Decimal | None = None
            away_price: Decimal | None = None
            home_point: Decimal | None = None
            for outcome in market.get("outcomes", ()) or ():
                if not isinstance(outcome, Mapping):
                    continue
                name = str(outcome.get("name") or "").strip()
                price = _decimal(outcome.get("price"))
                point = _decimal(outcome.get("point"))
                if not name or price is None or point is None or price <= 0:
                    continue
                if name == home:
                    home_price, home_point = price, point
                elif name == away:
                    away_price = price
            if home_price is None or away_price is None or home_point is None:
                continue
            bucket = grouped.setdefault(home_point, {"home": [], "away": []})
            bucket["home"].append(home_price)
            bucket["away"].append(away_price)
    if not grouped:
        return None
    # 选取出现次数最多的 home_point 作为基准；并列时取数值最小的（更窄 line 优先）。
    chosen_point = max(grouped.keys(), key=lambda pt: (len(grouped[pt]["home"]), -pt))
    bucket = grouped[chosen_point]
    home_prices = bucket["home"]
    away_prices = bucket["away"]
    if not home_prices or not away_prices:
        return None
    home_mean = sum(home_prices) / Decimal(len(home_prices))
    away_mean = sum(away_prices) / Decimal(len(away_prices))
    return home_mean, away_mean, chosen_point


__all__ = [
    "GameOddsClient",
    "GameOddsSnapshot",
    "GameSpreadSnapshot",
    "TheOddsApiGameOddsClient",
    "parse_theoddsapi_h2h_payload",
    "parse_theoddsapi_spreads_payload",
]
