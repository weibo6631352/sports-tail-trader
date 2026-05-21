"""Goalserve inplay WebSocket 客户端。

认证流程：
  1. POST http://live.goalserve.com/api/v1/auth/gettoken {"apiKey": "..."}
     → JWT token，有效期 60 分钟；过期前 5 分钟自动刷新。
  2. ws://live.goalserve.com/ws/{sport}?tkn={token}
     → 服务端推送 avl（初始全量）和 updt（增量更新）消息。

消息类型：
  avl  连接后批量推送，首次出现的赛事。
  updt 赛事更新；stp=99 表示赛事已移除。

内部架构：
  每个 sport 独立后台 Task，断连自动退避重连。
  _state: {sport: {event_id: event_dict}} — 当前活跃赛事内存快照。
  list_events() 读快照，与 SportsLiveAggregateClient 接口兼容。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import websockets
import websockets.exceptions

from polymarket_trader.domain.sports_live import (
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
    SportsLiveSourceStatus,
)
from polymarket_trader.infra.sports.common import utc_now
from polymarket_trader.infra.sports.goalserve_parsers import parse_goalserve_ws_events

logger = logging.getLogger(__name__)

_TOKEN_URL = "http://live.goalserve.com/api/v1/auth/gettoken"
_WS_BASE_URL = "ws://live.goalserve.com/ws"
_TOKEN_REFRESH_MARGIN_S = 300  # 过期前 5 分钟刷新
_RECONNECT_BASE_S = 5.0
_RECONNECT_MAX_S = 60.0
_STALE_THRESHOLD_S = 30.0  # 超过此时间无消息视为源失活

# Token 持久化路径：避免重启时重新申请占用 token slot 导致 429
_TOKEN_CACHE_PATH = Path(os.environ.get("GOALSERVE_TOKEN_CACHE", ".dev-runtime/goalserve_token.json"))

_SUPPORTED_SPORTS = frozenset({
    "basketball", "soccer", "hockey", "baseball", "tennis",
    "esports", "amfootball", "volleyball",
})


class GoalserveClient:
    """Goalserve inplay WebSocket 客户端。

    每个运动维护一个持久 WS 连接；断连自动退避重连。
    Token 过期前 5 分钟自动刷新；401 时立即强制刷新。
    list_events() 读取内存快照，接口与旧 HTTP 轮询客户端兼容。
    """

    def __init__(
        self,
        *,
        api_key: str,
        sports: tuple[str, ...] = ("basketball", "soccer", "hockey", "baseball", "tennis", "esports"),
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_key = api_key
        self._sports = tuple(s for s in sports if s in _SUPPORTED_SPORTS)
        self._now_provider = now_provider

        self._token: str | None = None
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()

        self._state: dict[str, dict[str, Any]] = {s: {} for s in self._sports}
        self._state_lock = asyncio.Lock()

        self._last_msg_time: dict[str, float] = {}
        self._consecutive_errors: dict[str, int] = {}

        self._tasks: list[asyncio.Task] = []
        self._started = False

    async def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        for sport in self._sports:
            task = asyncio.create_task(
                self._ws_loop(sport), name=f"goalserve_ws_{sport}"
            )
            self._tasks.append(task)

    async def _fetch_token(self) -> tuple[str, float]:
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            r = await client.post(_TOKEN_URL, json={"apiKey": self._api_key})
            r.raise_for_status()
            token = r.json()["token"]
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        exp = float(json.loads(base64.urlsafe_b64decode(payload_b64))["exp"])
        return token, exp

    def _load_cached_token(self) -> tuple[str, float] | None:
        try:
            data = json.loads(_TOKEN_CACHE_PATH.read_text())
            token, exp = data["token"], float(data["exp"])
            if time.time() < exp - _TOKEN_REFRESH_MARGIN_S:
                return token, exp
        except Exception:
            pass
        return None

    def _save_cached_token(self, token: str, exp: float) -> None:
        try:
            _TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _TOKEN_CACHE_PATH.write_text(json.dumps({"token": token, "exp": exp}))
        except Exception as exc:
            logger.warning("goalserve: failed to save token cache: %s", exc)

    async def _ensure_token(self) -> str:
        async with self._token_lock:
            if self._token is None or time.time() > self._token_exp - _TOKEN_REFRESH_MARGIN_S:
                # Try cached token from disk first to avoid consuming a token slot on restart
                cached = self._load_cached_token()
                if cached:
                    self._token, self._token_exp = cached
                    logger.info("goalserve: token loaded from cache, exp=%.0f", self._token_exp)
                else:
                    self._token, self._token_exp = await self._fetch_token()
                    self._save_cached_token(self._token, self._token_exp)
                    logger.info("goalserve: token refreshed, exp=%.0f", self._token_exp)
            assert self._token is not None  # invariant: always set by branches above
            return self._token

    async def _ws_loop(self, sport: str) -> None:
        delay = _RECONNECT_BASE_S
        while True:
            try:
                token = await self._ensure_token()
                url = f"{_WS_BASE_URL}/{sport}?tkn={token}"
                logger.info("goalserve: connecting WS for %s", sport)
                async with websockets.connect(
                    url, open_timeout=15, ping_interval=30, ping_timeout=10
                ) as ws:
                    delay = _RECONNECT_BASE_S
                    self._consecutive_errors[sport] = 0
                    logger.info("goalserve: WS connected for %s", sport)
                    async for raw in ws:
                        data = json.loads(raw)
                        await self._handle_message(sport, data)
            except asyncio.CancelledError:
                return
            except websockets.exceptions.InvalidStatus as exc:
                if exc.response.status_code == 401:
                    async with self._token_lock:
                        self._token = None
                    # Invalidate disk cache so next _ensure_token fetches fresh
                    try:
                        _TOKEN_CACHE_PATH.unlink(missing_ok=True)
                    except Exception:
                        pass
                    logger.warning("goalserve: WS 401 for %s, forcing token refresh", sport)
                else:
                    err = self._consecutive_errors.get(sport, 0) + 1
                    self._consecutive_errors[sport] = err
                    logger.warning("goalserve: WS %d for %s (err#%d)", exc.response.status_code, sport, err)
            except Exception as exc:
                err = self._consecutive_errors.get(sport, 0) + 1
                self._consecutive_errors[sport] = err
                logger.warning("goalserve: WS error for %s (err#%d): %s", sport, err, exc)

            await asyncio.sleep(min(delay, _RECONNECT_MAX_S))
            delay = min(delay * 1.5, _RECONNECT_MAX_S)

    async def _handle_message(self, sport: str, data: dict[str, Any]) -> None:
        mt = data.get("mt")
        if mt not in ("avl", "updt"):
            return
        event_id = str(data.get("id", ""))
        if not event_id:
            return

        self._last_msg_time[sport] = time.time()

        if data.get("stp") == 99:
            async with self._state_lock:
                self._state[sport].pop(event_id, None)
        else:
            async with self._state_lock:
                self._state[sport][event_id] = data

    async def list_events(self) -> SportsLiveSnapshot:
        await self._ensure_started()
        observed_at = utc_now(self._now_provider)
        now = time.time()

        async with self._state_lock:
            state_snapshot = {s: dict(d) for s, d in self._state.items()}

        all_events: list = []
        source_statuses: list[SportsLiveSourceStatus] = []

        for sport in self._sports:
            source_key = f"goalserve:{sport}"
            events_data = state_snapshot.get(sport, {})
            last_msg = self._last_msg_time.get(sport)
            n_errors = self._consecutive_errors.get(sport, 0)

            if n_errors > 3:
                health = SportsLiveSourceHealth.FAILED
            elif last_msg is not None and now - last_msg > _STALE_THRESHOLD_S:
                health = SportsLiveSourceHealth.FAILED
            elif events_data:
                health = SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
            else:
                health = SportsLiveSourceHealth.SUCCESS_EMPTY

            events = parse_goalserve_ws_events(sport, events_data, observed_at=observed_at)
            all_events.extend(events)
            source_statuses.append(
                SportsLiveSourceStatus(
                    source=source_key,
                    success=n_errors <= 3,
                    health=health,
                    events_seen=len(events_data),
                    observed_at=observed_at,
                    consecutive_failures=n_errors,
                )
            )

        return SportsLiveSnapshot(
            source="goalserve",
            observed_at=observed_at,
            events=tuple(all_events),
            source_statuses=tuple(source_statuses),
        )

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
