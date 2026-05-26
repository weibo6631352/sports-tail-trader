"""Goalserve 赛前赔率（Pregame Odds）客户端。

认证方式：API key 嵌入 URL，GZIP 压缩（httpx 自动处理 Content-Encoding: gzip）。
增量更新：首次拉取获得 ts 时间戳，后续请求携带 &ts=... 只拿变更部分。

输出：GoalservePregameSnapshot，包含每个运动的赔率列表，供workflow 层交叉验证 Polymarket 定价。
不走 SportsLiveSnapshot 路径（pregame 不是实时比赛状态）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from polymarket_trader.infra.sports.common import utc_now

_log = logging.getLogger(__name__)

# 运动 cat 参数映射：key 是内部运动名，value 是 Goalserve API 的 cat 参数值。
_SPORT_CATS: dict[str, str] = {
    "soccer": "soccer_10",
    "basketball": "basket_10",
    "tennis": "tennis_10",
    "hockey": "hockey_10",
    "handball": "handball_10",
    "volleyball": "volleyball_10",
    "amfootball": "football_10",
    "baseball": "baseball_10",
    "cricket": "cricket_10",
    "rugby": "rugby_10",
    "rugbyleague": "rugbyleague_10",
    "boxing": "boxing_10",
    "esports": "esports_10",
    "futsal": "futsal_10",
    "mma": "mma_10",
    "darts": "darts_10",
    "table_tennis": "table_tennis_10",
}

_MONEYLINE_NAMES = frozenset({"match winner", "1x2", "home/draw/away"})
_TOTALS_NAMES = frozenset({"over/under", "total goals", "total points"})


@dataclass(frozen=True, slots=True)
class PregameOutcome:
    name: str
    value_eu: Decimal        # 欧赔
    implied_prob: Decimal    # 1 / value_eu
    handicap: str            # 让分值（字符串，无让分时为空字符串）
    suspended: bool


@dataclass(frozen=True, slots=True)
class PregameMarket:
    market_id: int
    name: str                # "Match Winner", "Over/Under", "Asian Handicap" 等
    suspended: bool
    outcomes: tuple[PregameOutcome, ...]


@dataclass(frozen=True, slots=True)
class PregameMatch:
    match_id: str
    home_team: str
    away_team: str
    league: str
    start_time: datetime | None
    markets: tuple[PregameMarket, ...]
    sport: str

    def moneyline_market(self) -> PregameMarket | None:
        for market in self.markets:
            if market.name.lower() in _MONEYLINE_NAMES:
                return market
        return None

    def totals_market(self) -> PregameMarket | None:
        for market in self.markets:
            if market.name.lower() in _TOTALS_NAMES:
                return market
        return None


@dataclass(frozen=True, slots=True)
class GoalservePregameSnapshot:
    sport: str
    fetched_at: datetime
    ts: str | None                 # 下次增量请求用的时间戳
    matches: tuple[PregameMatch, ...]


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------

def _parse_decimal(value: Any) -> Decimal | None:
    """宽松解析赔率字符串为 Decimal，失败返回 None。"""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
        return d if d > 0 else None
    except InvalidOperation:
        return None


def _parse_bool_flag(value: Any) -> bool:
    """'0' / 0 / False → False；其他 → True。"""
    if value is None:
        return False
    return str(value).strip() not in {"0", "false", "False", ""}


def _parse_start_time(date_str: Any, time_str: Any) -> datetime | None:
    """将 '22.05.2025' + '15:00' 解析为 UTC-aware datetime（无时区信息的视为 UTC）。"""
    if not date_str or not time_str:
        return None
    try:
        dt_str = f"{date_str} {time_str}"
        dt = datetime.strptime(dt_str, "%d.%m.%Y %H:%M")
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _parse_outcome(raw: dict[str, Any]) -> PregameOutcome | None:
    """解析单个 outcome 字典。"""
    name = str(raw.get("name", "")).strip()
    odds_str = raw.get("odds") or raw.get("value")
    eu_odds = _parse_decimal(odds_str)
    if not name or eu_odds is None:
        return None
    implied = Decimal("1") / eu_odds
    handicap = str(raw.get("handicap") or raw.get("spread") or "").strip()
    suspended = _parse_bool_flag(raw.get("suspended"))
    return PregameOutcome(
        name=name,
        value_eu=eu_odds,
        implied_prob=implied,
        handicap=handicap,
        suspended=suspended,
    )


def _parse_market(raw: dict[str, Any]) -> PregameMarket | None:
    """解析单个 market 字典。"""
    try:
        market_id = int(str(raw.get("id", "0")))
    except (ValueError, TypeError):
        market_id = 0
    name = str(raw.get("name", "")).strip()
    suspended = _parse_bool_flag(raw.get("suspended"))

    raw_outcomes = raw.get("outcome") or []
    if isinstance(raw_outcomes, dict):
        raw_outcomes = [raw_outcomes]
    outcomes: list[PregameOutcome] = []
    for ro in raw_outcomes:
        if not isinstance(ro, dict):
            continue
        outcome = _parse_outcome(ro)
        if outcome is not None:
            outcomes.append(outcome)

    if not outcomes:
        return None
    return PregameMarket(
        market_id=market_id,
        name=name,
        suspended=suspended,
        outcomes=tuple(outcomes),
    )


def _parse_match(raw: dict[str, Any], league: str, sport: str) -> PregameMatch | None:
    """解析单个 match 字典（从第一个庄家取 markets）。"""
    match_id = str(raw.get("id", "")).strip()
    if not match_id:
        return None

    home_team = str((raw.get("localteam") or {}).get("name", "")).strip()
    away_team = str((raw.get("visitorteam") or {}).get("name", "")).strip()
    start_time = _parse_start_time(raw.get("date"), raw.get("time"))

    # 从第一个庄家取 markets
    odds_block = raw.get("odds") or {}
    bookmakers = odds_block.get("bookmaker") or []
    if isinstance(bookmakers, dict):
        bookmakers = [bookmakers]

    markets: list[PregameMarket] = []
    if bookmakers:
        first_bookmaker = bookmakers[0] if isinstance(bookmakers[0], dict) else {}
        raw_markets = first_bookmaker.get("market") or []
        if isinstance(raw_markets, dict):
            raw_markets = [raw_markets]
        for rm in raw_markets:
            if not isinstance(rm, dict):
                continue
            market = _parse_market(rm)
            if market is not None:
                markets.append(market)

    return PregameMatch(
        match_id=match_id,
        home_team=home_team,
        away_team=away_team,
        league=league,
        start_time=start_time,
        markets=tuple(markets),
        sport=sport,
    )


def _parse_pregame_response(sport: str, payload: dict[str, Any], fetched_at: datetime) -> GoalservePregameSnapshot:
    """将 Goalserve pregame JSON 解析为 GoalservePregameSnapshot。"""
    scores = payload.get("scores") or {}
    ts: str | None = str(scores["ts"]) if scores.get("ts") else None

    categories = scores.get("category") or []
    if isinstance(categories, dict):
        categories = [categories]

    matches: list[PregameMatch] = []
    for category in categories:
        if not isinstance(category, dict):
            continue
        league = str(category.get("name", "")).strip()
        raw_matches = category.get("match") or []
        if isinstance(raw_matches, dict):
            raw_matches = [raw_matches]
        for rm in raw_matches:
            if not isinstance(rm, dict):
                continue
            match = _parse_match(rm, league=league, sport=sport)
            if match is not None:
                matches.append(match)

    return GoalservePregameSnapshot(
        sport=sport,
        fetched_at=fetched_at,
        ts=ts,
        matches=tuple(matches),
    )


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class GoalservePregameOddsClient:
    """Goalserve 赛前赔率客户端。

    API key 嵌入 URL，GZIP 自动解压，并发拉取所有运动端点。
    单运动失败不影响其他运动（asyncio.gather with return_exceptions）。
    proxy 参数仅用于开发环境（本机 Clash 代理）；生产直连即可。
    """

    def __init__(
        self,
        *,
        api_key: str,
        sports: tuple[str, ...],
        base_url: str = "http://www.goalserve.com",
        timeout_s: float = 30.0,
        proxy: str | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_key = api_key
        self._sports = tuple(s.lower().strip() for s in sports if s.lower().strip() in _SPORT_CATS)
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_s)
        self._now_provider = now_provider

        mounts: dict[str, Any] | None = None
        if proxy:
            mounts = {
                "http://": httpx.AsyncHTTPTransport(proxy=proxy),
                "https://": httpx.AsyncHTTPTransport(proxy=proxy),
            }
        self._client = httpx.AsyncClient(
            mounts=mounts,
            timeout=self._timeout,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            # 允许 httpx 自动解压 gzip
            headers={"Accept-Encoding": "gzip, deflate"},
        )

    def _build_url(self, sport: str, *, ts: str | None = None) -> str:
        """构造赛前赔率请求 URL（API key 嵌 URL，加 json=1 获取 JSON 格式）。"""
        cat = _SPORT_CATS[sport]
        url = f"{self._base_url}/getfeed/{self._api_key}/getodds/{sport}?cat={cat}&json=1"
        if ts:
            url = f"{url}&ts={ts}"
        return url

    async def fetch_sport(self, sport: str, *, ts: str | None = None) -> GoalservePregameSnapshot:
        """拉取单运动赛前赔率快照。失败时返回空 snapshot 并记录错误。"""
        fetched_at = utc_now(self._now_provider)
        sport = sport.lower().strip()
        if sport not in _SPORT_CATS:
            _log.warning("goalserve_pregame: unsupported sport=%s", sport)
            return GoalservePregameSnapshot(sport=sport, fetched_at=fetched_at, ts=None, matches=())

        url = self._build_url(sport, ts=ts)
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            payload = response.json()
            snapshot = _parse_pregame_response(sport, payload, fetched_at)
            _log.debug(
                "goalserve_pregame: sport=%s matches=%d ts=%s",
                sport,
                len(snapshot.matches),
                snapshot.ts,
            )
            return snapshot
        except Exception as exc:
            _log.error("goalserve_pregame: sport=%s fetch failed: %s", sport, exc)
            return GoalservePregameSnapshot(sport=sport, fetched_at=fetched_at, ts=None, matches=())

    async def fetch_all(
        self, *, ts_by_sport: dict[str, str] | None = None
    ) -> dict[str, GoalservePregameSnapshot]:
        """并发拉取所有启用运动的赛前赔率，返回 {sport: snapshot} 映射。

        单运动失败返回空 snapshot，不阻断其他运动。
        ts_by_sport 携带上次拉取的时间戳，触发增量模式。
        """
        ts_map = ts_by_sport or {}
        tasks = [self.fetch_sport(sport, ts=ts_map.get(sport)) for sport in self._sports]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        snapshots: dict[str, GoalservePregameSnapshot] = {}
        fetched_at = utc_now(self._now_provider)
        for sport, result in zip(self._sports, results):
            if isinstance(result, Exception):
                _log.error("goalserve_pregame: sport=%s unexpected error: %s", sport, result)
                snapshots[sport] = GoalservePregameSnapshot(
                    sport=sport, fetched_at=fetched_at, ts=None, matches=()
                )
            else:
                snapshots[sport] = result
        return snapshots

    async def aclose(self) -> None:
        await self._client.aclose()
