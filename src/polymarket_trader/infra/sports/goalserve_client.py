"""Goalserve inplay feed HTTP 客户端。

认证方式：IP 白名单（无需 API key 在 URL 中），服务端返回 HTTP gzip 压缩 JSON。
httpx 自动处理 Content-Encoding: gzip，无需手动 gzip.decompress。

并发策略：
  - 每次 list_events() 并发拉取所有启用运动的端点（asyncio.gather）
  - 单运动超时/失败不阻塞其他运动
  - 连接池由 httpx.AsyncClient 管理（默认 max_connections=10）
"""

from __future__ import annotations

import asyncio
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
from polymarket_trader.infra.sports.goalserve_parsers import parse_goalserve_sport

_SPORT_URLS: dict[str, str] = {
    "basketball": "http://inplay.goalserve.com/inplay-basket.gz",
    "soccer": "http://inplay.goalserve.com/inplay-soccer.gz",
    "hockey": "http://inplay.goalserve.com/inplay-hockey.gz",
    "baseball": "http://inplay.goalserve.com/inplay-baseball.gz",
    "tennis": "http://inplay.goalserve.com/inplay-tennis.gz",
    "esports": "http://inplay.goalserve.com/inplay-esports.gz",
    "amfootball": "http://inplay.goalserve.com/inplay-amfootball.gz",
    "volleyball": "http://inplay.goalserve.com/inplay-volleyball.gz",
}


class GoalserveClient:
    """Goalserve inplay feed 客户端。

    IP 白名单认证，HTTP gzip 自动解压，并发拉取所有运动端点。
    proxy 参数仅用于开发环境（本机 Clash 代理），生产直连 IP 白名单即可。
    """

    def __init__(
        self,
        *,
        sports: tuple[str, ...] = ("basketball", "soccer", "hockey", "baseball", "tennis", "esports"),
        timeout_s: float = 8.0,
        proxy: str | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._sports = tuple(s.lower().strip() for s in sports if s.strip().lower() in _SPORT_URLS)
        self._timeout = httpx.Timeout(timeout_s)
        self._now_provider = now_provider
        if proxy:
            mounts: dict[str, Any] = {"http://": httpx.AsyncHTTPTransport(proxy=proxy), "https://": httpx.AsyncHTTPTransport(proxy=proxy)}
            self._client = httpx.AsyncClient(
                mounts=mounts,
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        else:
            # trust_env=False: 忽略 ALL_PROXY/HTTP_PROXY 等系统代理，
            # inplay.goalserve.com 需要 IP 白名单直连，系统代理会导致 403/超时。
            self._client = httpx.AsyncClient(
                trust_env=False,
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_events(self) -> SportsLiveSnapshot:
        """并发拉取所有运动 feed，合并返回统一快照。单运动失败记入 source_statuses 但不阻断其他。"""
        observed_at = utc_now(self._now_provider)

        tasks = [self._fetch_sport(sport, observed_at) for sport in self._sports]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_events = []
        source_statuses: list[SportsLiveSourceStatus] = []

        for sport, result in zip(self._sports, results):
            source_key = f"goalserve:{sport}"
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
            events, event_count = result
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
                    events_seen=event_count,
                    observed_at=observed_at,
                )
            )

        return SportsLiveSnapshot(
            source="goalserve",
            observed_at=observed_at,
            events=tuple(all_events),
            source_statuses=tuple(source_statuses),
        )

    async def _fetch_sport(
        self, sport: str, observed_at: datetime
    ) -> tuple[list, int]:
        """拉取单运动 inplay feed，解析后返回 (events, raw_event_count)。"""
        url = _SPORT_URLS[sport]
        response = await self._client.get(url)
        response.raise_for_status()
        data = response.json()
        raw_count = len(data.get("events", {}))
        events = parse_goalserve_sport(sport, data, observed_at=observed_at)
        return events, raw_count
