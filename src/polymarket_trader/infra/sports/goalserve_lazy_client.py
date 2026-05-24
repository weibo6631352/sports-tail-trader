"""Goalserve 低频 API 的 lazy fetch + cache 客户端（schedule/standings/h2h/injuries）。

设计：
- 不启后台 task（这些 API 数据每日更新，按需 fetch + cache 1h 足够）
- 每个 API 一个简单函数，调用即 fetch（不命中 cache 时）
- in-memory cache，重启清零
- 全部走 HTTP HTTPS，无 WS

API 覆盖：
- /baseball/mlb_shedule         - MLB 全赛季 fixtures
- /baseball/mlb_standings       - MLB 排名（强弱队 prior）
- /bsktbl/nba-standings         - NBA 排名
- /bsktbl/wnba-standings        - WNBA 排名
- /standings/{leagueId}          - Soccer 联赛排名
- /h2h/{team1_id}/{team2_id}    - 历史对决
- /soccernew/injuries           - Soccer 伤病
"""
from __future__ import annotations

import time
import asyncio
from typing import Any

import httpx

_DEFAULT_TIMEOUT_S = 15.0
_DEFAULT_CACHE_TTL_S = 3600.0  # 1h


class GoalserveLazyClient:
    """低频 goalserve API 统一 lazy fetcher + cache。

    使用：
        client = GoalserveLazyClient(api_key=..., proxy=...)
        await client.mlb_schedule()
        await client.mlb_standings()
        await client.h2h(team1_id="123", team2_id="456")
    """

    def __init__(
        self,
        *,
        api_key: str,
        proxy: str | None = None,
        cache_ttl_s: float = _DEFAULT_CACHE_TTL_S,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._base = f"https://www.goalserve.com/getfeed/{api_key}"
        self._cache_ttl_s = cache_ttl_s
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()
        if proxy:
            mounts = {
                "http://": httpx.AsyncHTTPTransport(proxy=proxy),
                "https://": httpx.AsyncHTTPTransport(proxy=proxy),
            }
            self._client = httpx.AsyncClient(mounts=mounts, timeout=timeout_s, trust_env=False)
        else:
            self._client = httpx.AsyncClient(timeout=timeout_s, trust_env=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_cached(self, path: str) -> dict:
        """Fetch + cache（cache_ttl_s 内复用）。"""
        async with self._lock:
            cached = self._cache.get(path)
            if cached and (time.time() - cached[0]) < self._cache_ttl_s:
                return cached[1]
        url = f"{self._base}/{path}"
        try:
            r = await self._client.get(url)
            if r.status_code != 200:
                return {"error": f"http {r.status_code}", "url": url}
            data = r.json()
            async with self._lock:
                self._cache[path] = (time.time(), data)
                # 限制 cache 大小
                if len(self._cache) > 100:
                    oldest = min(self._cache.items(), key=lambda x: x[1][0])
                    self._cache.pop(oldest[0], None)
            return data
        except Exception as exc:
            return {"error": str(exc), "url": url}

    async def mlb_schedule(self) -> dict[str, Any]:
        return await self._get_cached("baseball/mlb_shedule?json=1")

    async def mlb_standings(self) -> dict[str, Any]:
        return await self._get_cached("baseball/mlb_standings?json=1")

    async def nba_standings(self) -> dict[str, Any]:
        return await self._get_cached("bsktbl/nba-standings?json=1")

    async def wnba_standings(self) -> dict[str, Any]:
        return await self._get_cached("bsktbl/wnba-standings?json=1")

    async def soccer_standings(self, league_id: str) -> dict[str, Any]:
        return await self._get_cached(f"standings/{league_id}?json=1")

    async def h2h(self, team1_id: str, team2_id: str) -> dict[str, Any]:
        return await self._get_cached(f"h2h/{team1_id}/{team2_id}?json=1")

    async def soccer_injuries(self) -> dict[str, Any]:
        return await self._get_cached("soccernew/injuries?json=1")

    def cache_status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "cache_size": len(self._cache),
            "entries": [
                {"path": p, "age_seconds": round(now - ts, 1), "data_size": len(str(d))}
                for p, (ts, d) in sorted(self._cache.items(), key=lambda x: x[1][0])
            ],
        }
