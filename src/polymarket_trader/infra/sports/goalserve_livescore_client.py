"""Goalserve getfeed livescore HTTP 客户端。

认证方式：API key 嵌入 URL（https://www.goalserve.com/getfeed/{api_key}/{sport_path}）。
与 inplay feed 不同：需要 key，响应格式因端点而异（XML 或 JSON），无 gzip 自动压缩。

所有支持的运动及其端点路径统一在 _SPORT_FEEDS 中定义，字段含义：
  path   — URL 后缀（跟在 {api_key}/ 后面）
  xml    — True 表示响应为 XML，False 表示 JSON（加 ?json=1）

并发策略：
  - 每次 list_events() 并发拉取所有运动（asyncio.gather with return_exceptions）
  - 单运动失败不阻塞其他
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from polymarket_trader.domain.sports_live import (
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
    SportsLiveSourceStatus,
)
from polymarket_trader.infra.sports.common import utc_now
from polymarket_trader.infra.sports.goalserve_livescore_parsers import parse_goalserve_livescore_sport

# (path, xml) — path is appended after {api_key}/; xml=True → parse as XML, False → ?json=1
_SPORT_FEEDS: dict[str, tuple[str, bool]] = {
    "soccer":           ("soccernew/home",        True),   # all leagues incl Copa Libertadores/Sudamericana
    "basketball":       ("basketball/home",        True),   # international leagues
    "baseball":         ("baseball/home",          True),
    "hockey":           ("hockey/home",            True),
    "nba":              ("bsktbl/nba-scores",      True),
    "wnba":             ("bsktbl/wnba-scores",     True),
    "mlb":              ("baseball/mlb-scores",    True),
    "nhl":              ("hockey/nhl-scores",      True),
    "tennis":           ("tennis_scores/home",     True),
    "cricket":          ("cricket/livescore",      False),
    "handball":         ("handball/home",          False),
    "rugby":            ("rugby/home",             False),
    "boxing":           ("boxing/home",            False),
    "mma":              ("mma/live",               False),
    "golf_pga":         ("golf/live",              False),
    "golf_dp":          ("golf/european_live",     False),
    "golf_liv":         ("golf/liv_live",          False),
    "golf_lpga":        ("golf/lpga_live",         False),
    "horse_racing_us":  ("racing/usa",             False),
    "horse_racing_uk":  ("racing/uk",              False),
    "horse_racing_au":  ("racing/australia",       False),
    "horse_racing_hk":  ("racing/hk",             False),
    "f1":               ("f1/f1-live",             False),
    "motogp":           ("motors/motogp-live",     False),
}

_BASE_URL = "https://www.goalserve.com/getfeed"


class GoalserveLivescoreClient:
    """Goalserve getfeed livescore 客户端。

    API key 认证，并发拉取所有启用运动端点，输出 SportsLiveSnapshot。
    trust_env=False 确保不使用系统代理（系统 SOCKS5 会导致 goalserve.com 超时）。
    proxy 参数仅用于显式覆盖，优先于 trust_env。
    """

    def __init__(
        self,
        *,
        api_key: str,
        sports: tuple[str, ...] | None = None,
        base_url: str = _BASE_URL,
        timeout_s: float = 15.0,
        proxy: str | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_key = api_key
        # None means all supported sports; explicit tuple filters to known entries only.
        self._sports = tuple(_SPORT_FEEDS) if sports is None else tuple(s for s in sports if s in _SPORT_FEEDS)
        self._base_url = base_url.rstrip("/")
        self._now_provider = now_provider
        if proxy:
            mounts: dict[str, Any] = {
                "http://": httpx.AsyncHTTPTransport(proxy=proxy),
                "https://": httpx.AsyncHTTPTransport(proxy=proxy),
            }
            self._client = httpx.AsyncClient(
                mounts=mounts,
                timeout=httpx.Timeout(timeout_s),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        else:
            # trust_env=False: 忽略 ALL_PROXY/HTTP_PROXY 等系统代理环境变量，
            # 系统 SOCKS5 会导致 www.goalserve.com 连接超时。
            self._client = httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(timeout_s),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_events(self) -> SportsLiveSnapshot:
        """并发拉取所有运动 livescore feed，合并返回统一快照。单运动失败记入 source_statuses 但不阻断其他。"""
        observed_at = utc_now(self._now_provider)

        tasks = [self._fetch_sport(sport, observed_at) for sport in self._sports]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_events = []
        source_statuses: list[SportsLiveSourceStatus] = []

        for sport, result in zip(self._sports, results):
            source_key = f"goalserve_livescore:{sport}"
            if isinstance(result, Exception):
                source_statuses.append(
                    SportsLiveSourceStatus(
                        source=source_key,
                        success=False,
                        health=SportsLiveSourceHealth.FAILED,
                        events_seen=0,
                        observed_at=observed_at,
                        last_error=str(result),
                    )
                )
                continue
            events, raw_count = result
            all_events.extend(events)
            health = (
                SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
                if events
                else SportsLiveSourceHealth.SUCCESS_EMPTY
            )
            source_statuses.append(
                SportsLiveSourceStatus(
                    source=source_key,
                    success=True,
                    health=health,
                    events_seen=raw_count,
                    observed_at=observed_at,
                )
            )

        return SportsLiveSnapshot(
            source="goalserve_livescore",
            observed_at=observed_at,
            events=tuple(all_events),
            source_statuses=tuple(source_statuses),
        )

    async def _fetch_sport(self, sport: str, observed_at: datetime) -> tuple[list, int]:
        path, is_xml = _SPORT_FEEDS[sport]
        if is_xml:
            url = f"{self._base_url}/{self._api_key}/{path}"
            response = await self._client.get(url)
            response.raise_for_status()
            data = _xml_to_livescore_dict(response.content)
        else:
            url = f"{self._base_url}/{self._api_key}/{path}?json=1"
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()
        events = parse_goalserve_livescore_sport(sport, data, observed_at=observed_at)
        return events, len(events)


def _xml_to_livescore_dict(xml_bytes: bytes) -> dict[str, Any]:
    """把 Goalserve XML feed 转换为 parser 期望的 {"scores": {category: [...]}} dict。

    XML 格式（以 basketball/hockey 为例，tennis 用 player 替代 localteam/awayteam）：
      <scores sport="...">
        <category name="..." gid="..." id="...">
          <match ... >
            <localteam name="..." totalscore="..." />  OR <hometeam .../>
            <awayteam name="..." />
            <player name="..." s1="..." ... />  (tennis)
          </match>
        </category>
      </scores>

    对应 dict 格式（与 getfeed JSON 一致）：
      {"scores": {"category": [{"name": ..., "match": [{...}]}]}}

    同一 tag 多次出现时（如 tennis <player>）存为列表 _tag_list。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return {}

    categories: list[dict[str, Any]] = []
    for cat_el in root.findall("category"):
        cat_dict: dict[str, Any] = dict(cat_el.attrib)
        matches: list[dict[str, Any]] = []
        # Soccer XML has an intermediate <matches date="..."> wrapper; other sports put
        # <match> directly under <category>. Search both levels.
        match_els = cat_el.findall("match")
        if not match_els:
            for matches_wrapper in cat_el.findall("matches"):
                match_els.extend(matches_wrapper.findall("match"))
        for match_el in match_els:
            match_dict: dict[str, Any] = dict(match_el.attrib)
            # 按 tag 分组，支持 tennis 的 <player> 多子节点
            tag_counts: dict[str, int] = {}
            for child in match_el:
                tag_counts[child.tag] = tag_counts.get(child.tag, 0) + 1
            seen: dict[str, int] = {}
            for child in match_el:
                child_dict: dict[str, Any] = dict(child.attrib)
                sub_children = list(child)
                if sub_children:
                    child_dict["_children"] = [
                        {**dict(sc.attrib), "_tag": sc.tag} for sc in sub_children
                    ]
                tag = child.tag
                if tag_counts[tag] > 1:
                    # 多个同名子节点 → 追加到 list
                    list_key = f"_{tag}_list"
                    if list_key not in match_dict:
                        match_dict[list_key] = []
                    match_dict[list_key].append(child_dict)
                else:
                    match_dict[tag] = child_dict
                seen[tag] = seen.get(tag, 0) + 1
            matches.append(match_dict)
        cat_dict["match"] = matches
        categories.append(cat_dict)

    return {"scores": {"category": categories}}
