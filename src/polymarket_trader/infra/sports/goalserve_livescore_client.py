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
import logging
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

logger = logging.getLogger(__name__)

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
    "esports":          ("esports/home",           False),   # inplay WS 不在套餐内（403），livescore getfeed 可用
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

# 策略侧 _market_sport_codes 输出的规范运动码 → 本文件 _SPORT_FEEDS key 集合。
# 用于 demand-driven 轮询：只有当某个规范码对应的 feed key 集合里至少有一个
# 被 active_sports_provider 选中时，才真正发起该 sport 的 HTTP 抓取。
# 没有 livescore feed 的运动（american-football / table-tennis / volleyball）
# 映射到空集——它们不会触发任何 livescore 抓取。
SPORT_CODE_TO_FEED_KEYS: dict[str, frozenset[str]] = {
    "esports":          frozenset({"esports"}),
    "football":         frozenset({"soccer"}),
    "ice-hockey":       frozenset({"hockey", "nhl"}),
    "baseball":         frozenset({"baseball", "mlb"}),
    "basketball":       frozenset({"basketball", "nba", "wnba"}),
    "tennis":           frozenset({"tennis"}),
    "cricket":          frozenset({"cricket"}),
    "rugby":            frozenset({"rugby"}),
    "handball":         frozenset({"handball"}),
    "mma":              frozenset({"mma"}),
    "boxing":           frozenset({"boxing"}),
    "golf":             frozenset({"golf_pga", "golf_dp", "golf_liv", "golf_lpga"}),
    "horse-racing":     frozenset(
        {"horse_racing_us", "horse_racing_uk", "horse_racing_au", "horse_racing_hk"}
    ),
    "formula1":         frozenset({"f1"}),
    "motogp":           frozenset({"motogp"}),
    "american-football": frozenset(),
    "table-tennis":     frozenset(),
    "volleyball":       frozenset(),
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
        poll_interval_s: float = 5.0,
        proxy: str | None = None,
        now_provider: Callable[[], datetime] | None = None,
        active_sports_provider: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._api_key = api_key
        # None means all supported sports; explicit tuple filters to known entries only.
        self._sports = tuple(_SPORT_FEEDS) if sports is None else tuple(s for s in sports if s in _SPORT_FEEDS)
        self._base_url = base_url.rstrip("/")
        self._now_provider = now_provider
        # demand-driven 轮询：每轮抓取前调用此回调，只抓回调返回集合中的 sport。
        # None → 退回到无条件全量轮询（向后兼容）。回调必须廉价（每轮都调）。
        self._active_sports_provider = active_sports_provider
        self._poll_interval_s = poll_interval_s
        # 整轮抓取硬超时：单 httpx 请求各有 timeout，但代理半死时整轮 gather
        # 仍可能卡死。没有外层超时，后台轮询会永久僵死、缓存永不刷新。
        self._fetch_round_timeout_s = max(60.0, timeout_s * 4)
        # Background polling: _cache holds the last successful snapshot; _poll_task is the
        # background loop. list_events() returns cached data without blocking on HTTP.
        self._cache: SportsLiveSnapshot | None = None
        self._poll_task: asyncio.Task[None] | None = None
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

    def livescore_per_sport_status(self) -> list[dict[str, Any]]:
        """每个 sport HTTP 轮询状态快照（读缓存，不触发 I/O，仅用于观测）。"""
        cache = self._cache
        poll_age_s: float | None = None
        if cache is not None and cache.observed_at is not None:
            import time as _time
            poll_age_s = round(_time.time() - cache.observed_at.timestamp(), 1)

        status_by_sport: dict[str, SportsLiveSourceStatus] = {}
        if cache is not None:
            for ss in cache.source_statuses:
                # source format: "goalserve_livescore:{sport}"
                if ":" in ss.source:
                    status_by_sport[ss.source.split(":", 1)[1]] = ss

        result = []
        for sport in self._sports:
            ss = status_by_sport.get(sport)
            if ss is not None:
                connected = ss.success
                events = ss.events_seen
                last_error = ss.last_error
                idle = False
            else:
                # demand-driven 轮询下，本轮没需求的 sport 不会出现在 source_statuses
                # 里。这不是错误，而是"按需空闲"——标记 idle，connected=True，避免被
                # 观测面板误判为断流。仅首次缓存为空且尚未抓取时才算未连接。
                connected = cache is not None
                events = 0
                last_error = None
                idle = cache is not None
            result.append({
                "sport": sport,
                "type": "http",
                "connected": connected,
                "idle": idle,
                "events": events,
                "last_error": last_error,
                "poll_age_s": poll_age_s,
                "poll_task_running": self._poll_task is not None and not self._poll_task.done(),
            })
        return result

    async def aclose(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._client.aclose()

    async def list_events(self) -> SportsLiveSnapshot:
        """返回最近一次后台轮询的快照，不阻塞在 HTTP 请求上。

        首次调用（缓存为空）同步拉取一次并启动后台轮询任务；此后每次调用立即
        返回内存缓存，sync_once 不再被 HTTP 延迟拖慢。

        每次调用都探测后台轮询任务是否已退出（崩溃 / 被取消）。任务一旦退出，
        缓存会永久冻结、livescore 数据源静默断流——这里检测并重启，保证自愈。
        """
        if self._cache is not None:
            self._ensure_poll_task_alive()
            return self._cache
        # First call: fetch synchronously so the caller has real data immediately.
        # provider 本轮返回空集时 _fetch_all_sports 返回 None；首次调用没有任何
        # 缓存可保留，因此用空快照兜底（后续轮询会在有需求时填充真实数据）。
        snapshot = await self._fetch_all_sports()
        if snapshot is None:
            snapshot = self._empty_snapshot()
        self._cache = snapshot
        # Kick off background loop for all subsequent calls.
        self._ensure_poll_task_alive()
        return snapshot

    def _empty_snapshot(self) -> SportsLiveSnapshot:
        """无 sport 被抓取时的空快照（仅用于首次调用兜底）。"""
        return SportsLiveSnapshot(
            source="goalserve_livescore",
            observed_at=utc_now(self._now_provider),
            events=(),
            source_statuses=(),
        )

    def _ensure_poll_task_alive(self) -> None:
        """启动后台轮询任务；任务已退出时记录原因并重启。"""

        prev = self._poll_task
        if prev is not None and not prev.done():
            return
        if prev is not None:
            reason = "cancelled" if prev.cancelled() else repr(prev.exception())
            logger.warning(
                "goalserve_livescore: poll task exited (%s), restarting", reason
            )
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="goalserve_livescore_poll"
        )

    async def _poll_loop(self) -> None:
        """后台持续轮询：每次 fetch 完立即更新缓存，再等 poll_interval_s。

        当 active_sports_provider 本轮返回空集时，_fetch_all_sports 返回 None，
        表示"本轮不抓取任何 sport"——此时必须保留上一份 _cache，不能用空快照
        覆盖。否则刚上线的市场会在下次 provider 刷新前丢掉 live state。
        """
        while True:
            await asyncio.sleep(self._poll_interval_s)
            try:
                # 整轮抓取设硬超时：代理半死时 gather 可能永不返回，
                # 没有外层超时会让本轮询永久僵死。
                snapshot = await asyncio.wait_for(
                    self._fetch_all_sports(), timeout=self._fetch_round_timeout_s
                )
                if snapshot is not None:
                    self._cache = snapshot
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("goalserve_livescore: poll error: %s", exc)

    def _sports_to_fetch(self) -> tuple[str, ...]:
        """本轮要抓取的 sport 集合。

        provider 为 None → 全量轮询（向后兼容旧行为）；
        provider 存在 → 只抓取「有需求」的 sport（有 live/即将开赛的 Polymarket
        市场映射到该 feed key）。provider 返回空集时返回空 tuple，调用方据此保留缓存。
        """
        if self._active_sports_provider is None:
            return self._sports
        active = self._active_sports_provider()
        return tuple(s for s in self._sports if s in active)

    async def _fetch_all_sports(self) -> SportsLiveSnapshot | None:
        """并发拉取需求内的运动 livescore feed，合并返回统一快照。

        单运动失败记入 source_statuses 但不阻断其他。
        本轮无任何 sport 需要抓取（demand-driven provider 返回空集）时返回 None——
        调用方据此保留上一份缓存，不用空快照覆盖刚上线市场的 live state。
        """
        observed_at = utc_now(self._now_provider)

        sports_to_fetch = self._sports_to_fetch()
        if not sports_to_fetch:
            return None

        tasks = [self._fetch_sport(sport, observed_at) for sport in sports_to_fetch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_events = []
        source_statuses: list[SportsLiveSourceStatus] = []

        for sport, result in zip(sports_to_fetch, results):
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
