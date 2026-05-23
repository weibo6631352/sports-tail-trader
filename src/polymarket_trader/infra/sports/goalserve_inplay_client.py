"""Goalserve inplay HTTP-GZIP feed 客户端。

数据源：``http://inplay.goalserve.com/inplay-{sport}.gz``——keyless（IP 白名单，
经 ``GOALSERVE_PROXY`` 代理出口），gunzip 后为 JSON，服务端每 1 秒刷新。
Goalserve 全部走 HTTP（官方 WebSocket 已弃用且对应客户端代码已删，不再尝试）。

速率限制（实测）：**同一运动 ~1 请求/秒**——同 sport 快于 ~1/s 触发 HTTP 429；
不同 sport 并发不共享预算。因此每个 sport 维护**独立后台轮询 Task**，各自按
``poll_interval_s``（默认 1.2s，留安全余量）节流，互不影响。429 时该 sport
单独退避一段时间，不波及其他 sport——这与 livescore 客户端"单轮 gather 全量"
的模型不同（livescore 端点无 per-sport 限流）。

接口与 ``GoalserveLivescoreClient`` 对齐，可直接接入 ``SportsLiveAggregateClient``：
  - ``list_events() -> SportsLiveSnapshot``：返回各 sport 最新缓存的合并快照；
  - ``aclose()``：取消所有后台 Task 并关闭 HTTP client；
  - ``inplay_per_sport_status()``：per-sport 轮询状态快照，供 admin 观测；
  - ``active_sports_provider``：demand-driven，仅轮询有需求的 sport。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from polymarket_trader.domain.sports_live import (
    LiveEvent,
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
    SportsLiveSourceStatus,
)
from polymarket_trader.infra.sports.common import utc_now
from polymarket_trader.infra.sports.goalserve_inplay_parsers import parse_goalserve_inplay

logger = logging.getLogger(__name__)

SOURCE = "goalserve_inplay"

# inplay feed 支持的 8 个运动（路径 token，与 URL 里的写法一致）。
_INPLAY_SPORTS: tuple[str, ...] = (
    "soccer",
    "basket",
    "tennis",
    "volleyball",
    "amfootball",
    "esports",
    "hockey",
    "baseball",
)

# 策略侧规范运动码 → inplay feed 路径 token 集合，用于 demand-driven 轮询：
# active_sports_provider 返回的规范码命中此映射时才轮询对应 feed token。
SPORT_CODE_TO_INPLAY_KEYS: dict[str, frozenset[str]] = {
    "football": frozenset({"soccer"}),
    "basketball": frozenset({"basket"}),
    "tennis": frozenset({"tennis"}),
    "volleyball": frozenset({"volleyball"}),
    "american-football": frozenset({"amfootball"}),
    "esports": frozenset({"esports"}),
    "ice-hockey": frozenset({"hockey"}),
    "baseball": frozenset({"baseball"}),
}

_BASE_URL = "http://inplay.goalserve.com"


@dataclass
class _SportPollState:
    """单个 sport 的后台轮询状态（缓存 + 健康 + 退避）。"""

    events: tuple[LiveEvent, ...] = ()
    raw_count: int = 0
    last_success_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    # 429 触发的退避截止时间戳（time.monotonic 基准）；None 表示无退避。
    backoff_until: float | None = None
    # demand-driven 下本轮无需求时标记 idle——非错误，避免观测面板误判断流。
    idle: bool = False
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class GoalserveInplayClient:
    """Goalserve inplay HTTP-GZIP feed 客户端。

    每个 sport 一个独立后台 Task：循环抓取 → gunzip → 解析 → 更新缓存 → 按
    ``poll_interval_s`` 节流。``list_events()`` 只读各 sport 缓存的合并结果，
    不阻塞在 HTTP 上。
    """

    def __init__(
        self,
        *,
        sports: tuple[str, ...] | None = None,
        base_url: str = _BASE_URL,
        proxy: str | None = None,
        timeout_s: float = 12.0,
        poll_interval_s: float = 1.2,
        rate_limit_backoff_s: float = 3.0,
        now_provider: Callable[[], datetime] | None = None,
        active_sports_provider: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        # None → 全部 8 个运动；显式 tuple 只保留已知 token。
        self._sports = (
            _INPLAY_SPORTS if sports is None else tuple(s for s in sports if s in _INPLAY_SPORTS)
        )
        self._base_url = base_url.rstrip("/")
        self._now_provider = now_provider
        self._active_sports_provider = active_sports_provider
        # 同 sport ~1 req/s 是硬限制——poll_interval 默认 1.2s 留安全余量。
        self._poll_interval_s = max(1.05, float(poll_interval_s))
        # 429 退避：实测同 sport 超速即 429，退避后再试。
        self._rate_limit_backoff_s = max(self._poll_interval_s, float(rate_limit_backoff_s))
        self._states: dict[str, _SportPollState] = {s: _SportPollState() for s in self._sports}
        self._started = False
        # monotonic 计时基准——退避用单调时钟，不受系统时钟跳变影响。
        self._monotonic = time.monotonic
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
            # trust_env=False：忽略系统 SOCKS5 代理，与 livescore 客户端同理。
            self._client = httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(timeout_s),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )

    # -- 观测 ----------------------------------------------------------------

    def inplay_per_sport_status(self) -> list[dict[str, Any]]:
        """每个 sport 的 HTTP 轮询状态快照（读缓存，不触发 I/O）。"""
        now = self._monotonic()
        result: list[dict[str, Any]] = []
        for sport in self._sports:
            st = self._states[sport]
            task_running = st.task is not None and not st.task.done()
            poll_age_s: float | None = None
            if st.last_success_at is not None:
                poll_age_s = round(
                    utc_now(self._now_provider).timestamp() - st.last_success_at.timestamp(), 1
                )
            backoff_remaining_s: float | None = None
            if st.backoff_until is not None and st.backoff_until > now:
                backoff_remaining_s = round(st.backoff_until - now, 1)
            result.append(
                {
                    "sport": sport,
                    "type": "http",
                    "connected": st.last_success_at is not None,
                    "idle": st.idle,
                    "events": st.raw_count,
                    "last_error": st.last_error,
                    "poll_age_s": poll_age_s,
                    "backoff_remaining_s": backoff_remaining_s,
                    "consecutive_failures": st.consecutive_failures,
                    "poll_task_running": task_running,
                }
            )
        return result

    # -- 生命周期 ------------------------------------------------------------

    async def aclose(self) -> None:
        """取消所有 sport 的后台 Task 并关闭 HTTP client。"""
        tasks = [st.task for st in self._states.values() if st.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._client.aclose()

    async def list_events(self) -> SportsLiveSnapshot:
        """返回各 sport 最新缓存的合并快照，不阻塞在 HTTP 上。

        首次调用启动所有 sport 的后台 Task；此后立即返回内存缓存。每次调用都
        探测后台 Task 是否退出（崩溃 / 取消），退出则重启，保证自愈。
        """
        self._ensure_tasks_alive()
        observed_at = utc_now(self._now_provider)
        all_events: list[LiveEvent] = []
        source_statuses: list[SportsLiveSourceStatus] = []
        for sport in self._sports:
            st = self._states[sport]
            all_events.extend(st.events)
            source_statuses.append(self._sport_status(sport, st, observed_at))
        return SportsLiveSnapshot(
            source=SOURCE,
            observed_at=observed_at,
            events=tuple(all_events),
            source_statuses=tuple(source_statuses),
        )

    def _sport_status(
        self, sport: str, st: _SportPollState, observed_at: datetime
    ) -> SportsLiveSourceStatus:
        source_key = f"{SOURCE}:{sport}"
        if st.idle:
            # demand-driven 下本轮无需求——按 CACHED 上报，不算失败也不算新鲜抓取。
            return SportsLiveSourceStatus(
                source=source_key,
                success=True,
                health=SportsLiveSourceHealth.CACHED,
                events_seen=st.raw_count,
                observed_at=st.last_success_at or observed_at,
            )
        if st.backoff_until is not None and st.backoff_until > self._monotonic():
            # 429 退避中——RATE_LIMITED 优先于 FAILED，明确区分限流与真失败；
            # 仍带上缓存事件数（退避期间复用上一份数据）。
            return SportsLiveSourceStatus(
                source=source_key,
                success=False,
                health=SportsLiveSourceHealth.RATE_LIMITED,
                events_seen=st.raw_count,
                observed_at=st.last_success_at or observed_at,
                last_error=st.last_error,
                consecutive_failures=st.consecutive_failures,
            )
        if st.last_success_at is None and st.last_error is not None:
            # 从未成功过且有非限流错误——FAILED。
            return SportsLiveSourceStatus(
                source=source_key,
                success=False,
                health=SportsLiveSourceHealth.FAILED,
                events_seen=0,
                observed_at=observed_at,
                last_error=st.last_error,
                consecutive_failures=st.consecutive_failures,
            )
        health = (
            SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
            if st.events
            else SportsLiveSourceHealth.SUCCESS_EMPTY
        )
        return SportsLiveSourceStatus(
            source=source_key,
            success=True,
            health=health,
            events_seen=st.raw_count,
            observed_at=st.last_success_at or observed_at,
        )

    def _ensure_tasks_alive(self) -> None:
        """启动 / 重启每个 sport 的后台轮询 Task。"""
        for sport in self._sports:
            st = self._states[sport]
            prev = st.task
            if prev is not None and not prev.done():
                continue
            if prev is not None:
                reason = "cancelled" if prev.cancelled() else repr(prev.exception())
                logger.warning(
                    "goalserve_inplay: poll task for %s exited (%s), restarting", sport, reason
                )
            st.task = asyncio.create_task(
                self._poll_loop(sport), name=f"goalserve_inplay_poll:{sport}"
            )
        self._started = True

    # -- 轮询 ----------------------------------------------------------------

    def _is_demanded(self, sport: str) -> bool:
        """demand-driven 判定：active_sports_provider 缺省时恒为 True（全量轮询）。"""
        if self._active_sports_provider is None:
            return True
        try:
            active = self._active_sports_provider()
        except Exception:
            # provider 异常不应让轮询停摆——回退到抓取。
            return True
        return any(sport in keys for keys in _matching_keys(active))

    async def _poll_loop(self, sport: str) -> None:
        """单 sport 后台轮询循环：抓取 → 解析 → 更新缓存 → 节流等待。

        429 时该 sport 单独进入 ``_rate_limit_backoff_s`` 退避，期间跳过抓取，
        缓存保留——不影响其他 sport。其他异常按 consecutive_failures 累计，
        但同样保留上一份缓存，避免一次抖动让该 sport 直播状态丢失。
        """
        st = self._states[sport]
        while True:
            interval = self._poll_interval_s
            try:
                if not self._is_demanded(sport):
                    st.idle = True
                else:
                    st.idle = False
                    now = self._monotonic()
                    if st.backoff_until is not None and st.backoff_until > now:
                        # 退避未到期——本轮跳过抓取，按退避剩余时长等待。
                        interval = max(self._poll_interval_s, st.backoff_until - now)
                    else:
                        await self._fetch_once(sport)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("goalserve_inplay: poll error for %s: %s", sport, exc)
            await asyncio.sleep(interval)

    async def _fetch_once(self, sport: str) -> None:
        """抓取并解析单个 sport 的 inplay feed，更新该 sport 缓存。"""
        st = self._states[sport]
        url = f"{self._base_url}/inplay-{sport}.gz"
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            st.consecutive_failures += 1
            st.last_error = f"transport error: {exc}"
            return

        if response.status_code == 429:
            # 同 sport 超速——单独退避，不波及其他 sport。
            st.consecutive_failures += 1
            st.last_error = "HTTP 429 rate limited"
            st.backoff_until = self._monotonic() + self._rate_limit_backoff_s
            return
        if response.status_code != 200:
            st.consecutive_failures += 1
            st.last_error = f"HTTP {response.status_code}"
            return

        try:
            feed = _decode_feed(response.content)
        except Exception as exc:
            st.consecutive_failures += 1
            st.last_error = f"decode error: {exc}"
            return

        observed_at = utc_now(self._now_provider)
        events = parse_goalserve_inplay(sport, feed, observed_at=observed_at)
        st.events = tuple(events)
        raw = feed.get("events")
        st.raw_count = len(raw) if isinstance(raw, dict) else len(events)
        st.last_success_at = observed_at
        st.last_error = None
        st.consecutive_failures = 0
        st.backoff_until = None


def _matching_keys(active: frozenset[str]) -> list[frozenset[str]]:
    """把 active 规范运动码集合映射成 inplay feed token 集合列表。"""
    return [SPORT_CODE_TO_INPLAY_KEYS.get(code, frozenset()) for code in active]


def _decode_feed(content: bytes) -> dict[str, Any]:
    """gunzip + json.loads inplay feed body。

    服务端发的是 ``.gz``——某些代理会自动解压，因此先试直接 json，再试 gunzip，
    两种形态都接受，保证代理行为差异不影响解析。
    """
    try:
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return json.loads(gzip.decompress(content))
