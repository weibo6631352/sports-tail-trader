"""Goalserve inplay WebSocket 客户端。

认证流程：
  1. POST http://live.goalserve.com/api/v1/auth/gettoken {"apiKey": "..."}
     → JWT token，有效期 60 分钟；过期前 5 分钟自动刷新。
  2. ws://live.goalserve.com/ws/{sport}?tkn={token}
     → 服务端推送 avl（初始全量）和 updt（增量更新）消息。

消息类型：
  avl  连接后批量推送，首次出现的赛事。
  updt 赛事更新；stp=99 表示赛事已移除。

生命周期管理：
  stp=99          → 立即驱逐（Goalserve 显式移除）
  stp∈{3,4,5}    → 终态：保留 _TERMINAL_HOLD_S 后驱逐（给策略最后评估窗口）
  断线重连孤立    → 重连后 _RECONNECT_GRACE_S 内未被 avl 重新确认的旧 event 驱逐
  绝对兜底        → 任何 event 超过 _ORPHAN_TTL_S 未更新直接驱逐

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
# 不带浏览器 UA 的 gettoken 请求会被边缘 WAF 拦成 401（空 body）；带上后才进到
# 真正的鉴权/限流逻辑。必须发送类浏览器请求头。
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://live.goalserve.com",
    "Referer": "https://live.goalserve.com/",
}
_TOKEN_REFRESH_MARGIN_S = 300   # 过期前 5 分钟刷新
# gettoken 失败（401/429 等）后的共享冷却：冷却期内所有 sport 直接跳过 gettoken，
# 不再各自重试。没有这个冷却，8 个 sport 各自每 60s 重试会持续打爆 gettoken 配额。
_TOKEN_FAILURE_COOLDOWN_S = 600
_RECONNECT_BASE_S = 5.0
_RECONNECT_MAX_S = 60.0
# 某运动 WS 返回 403（inplay 未授权该运动）后的长退避：重试无用，避免刷屏。
_SPORT_FORBIDDEN_BACKOFF_S = 1800.0
_STALE_THRESHOLD_S = 30.0       # 超过此时间无消息视为源失活

# stp 终态：ENDED=3, POSTPONED=4, CANCELLED=5
# 保留短暂窗口供策略评估，之后驱逐
_TERMINAL_STPS = frozenset({3, 4, 5})
_TERMINAL_HOLD_S = 600          # 10 分钟
# 重连后 avl 重新确认的宽限期；超时未确认视为孤立
_RECONNECT_GRACE_S = 120        # 2 分钟
# 绝对兜底：任何 event 超过此时长未收到更新直接驱逐
_ORPHAN_TTL_S = 21600           # 6 小时

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
        proxy: str | None = None,
        token_cache_path: Path | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_key = api_key
        self._sports = tuple(s for s in sports if s in _SUPPORTED_SPORTS)
        # live.goalserve.com 直连会被拒（401）；与 livescore 一样必须走代理。
        self._proxy = proxy
        self._token_cache_path = token_cache_path or _TOKEN_CACHE_PATH
        self._now_provider = now_provider

        self._token: str | None = None
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()
        # Token 生命周期观测：必须记录每次 gettoken 的次数/结果/占用，否则无从
        # 得知 token 槽占用情况与 WS 连接上限。
        self._token_acquired_at: float | None = None
        self._token_fetch_count = 0
        self._token_fetch_failures = 0
        self._last_token_attempt_at: float | None = None
        self._last_token_error: str | None = None
        self._token_cooldown_until: float = 0.0

        # event 状态快照：{sport: {event_id: ws_message_dict}}
        self._state: dict[str, dict[str, Any]] = {s: {} for s in self._sports}
        # 各 event 最后更新时间：{sport: {event_id: unix_timestamp}}
        self._state_seen_at: dict[str, dict[str, float]] = {s: {} for s in self._sports}
        # 各 event 最后已知 stp 值：{sport: {event_id: stp_int}}
        self._state_stp: dict[str, dict[str, int]] = {s: {} for s in self._sports}
        self._state_lock = asyncio.Lock()

        self._last_msg_time: dict[str, float] = {}
        self._consecutive_errors: dict[str, int] = {}
        # 各 sport 最近一次 WS 建连时间（用于重连孤立检测）
        self._reconnect_at: dict[str, float] = {}

        self._tasks: list[asyncio.Task] = []
        self._started = False

        # 启动即恢复 token 占用记录，重启后仍知晓槽位占用与累计 gettoken。
        self._restore_token_record()

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
        # trust_env=False 屏蔽系统 SOCKS 代理；proxy 显式走配置的 HTTP 代理。
        # 必带浏览器请求头，否则被边缘 WAF 拦成 401。
        async with httpx.AsyncClient(trust_env=False, timeout=15, proxy=self._proxy) as client:
            r = await client.post(
                _TOKEN_URL,
                json={"apiKey": self._api_key},
                headers=_BROWSER_HEADERS,
            )
            r.raise_for_status()
            token = r.json()["token"]
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        exp = float(json.loads(base64.urlsafe_b64decode(payload_b64))["exp"])
        return token, exp

    def _restore_token_record(self) -> None:
        """启动时从磁盘恢复 token 占用记录。

        token 槽在 token 过期前一直被占用（即使本进程已重启）。持久化完整记录
        让重启后仍能知道：是否还持有有效 token、累计 gettoken 次数/失败、冷却是否
        仍生效——避免重启后盲目重新申请打爆 token 槽。
        """

        try:
            data = json.loads(self._token_cache_path.read_text())
        except Exception:
            return
        try:
            self._token_fetch_count = int(data.get("gettoken_count", 0))
            self._token_fetch_failures = int(data.get("gettoken_failures", 0))
            self._last_token_error = data.get("last_error")
            attempt = data.get("last_attempt_at")
            self._last_token_attempt_at = None if attempt is None else float(attempt)
            cooldown = float(data.get("cooldown_until", 0.0))
            if cooldown > time.time():
                self._token_cooldown_until = cooldown
            token = data.get("token")
            exp = data.get("exp")
            if token and exp is not None and time.time() < float(exp) - _TOKEN_REFRESH_MARGIN_S:
                self._token = str(token)
                self._token_exp = float(exp)
                acquired = data.get("acquired_at")
                self._token_acquired_at = None if acquired is None else float(acquired)
                logger.info(
                    "goalserve: inplay token record restored — token still valid, exp=%.0f "
                    "(cumulative gettoken=%d failures=%d)",
                    self._token_exp, self._token_fetch_count, self._token_fetch_failures,
                )
            else:
                logger.info(
                    "goalserve: inplay token record restored — no valid token "
                    "(cumulative gettoken=%d failures=%d cooldown_remaining=%.0fs)",
                    self._token_fetch_count, self._token_fetch_failures,
                    max(0.0, self._token_cooldown_until - time.time()),
                )
        except Exception as exc:
            logger.warning("goalserve: failed to restore token record: %s", exc)

    def _persist_token_record(self) -> None:
        """把当前 token 占用记录写盘，供重启后恢复。"""

        try:
            self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_cache_path.write_text(json.dumps({
                "token": self._token,
                "exp": self._token_exp,
                "acquired_at": self._token_acquired_at,
                "gettoken_count": self._token_fetch_count,
                "gettoken_failures": self._token_fetch_failures,
                "last_attempt_at": self._last_token_attempt_at,
                "last_error": self._last_token_error,
                "cooldown_until": self._token_cooldown_until,
            }))
        except Exception as exc:
            logger.warning("goalserve: failed to persist token record: %s", exc)

    async def _ensure_token(self) -> str:
        async with self._token_lock:
            # 内存中已有有效 token（含启动时从持久记录恢复的）→ 直接复用，不占新槽。
            if self._token is not None and time.time() <= self._token_exp - _TOKEN_REFRESH_MARGIN_S:
                return self._token
            # 共享冷却：一个 sport 失败后，其余 sport 在冷却期内不再各自打 gettoken。
            now = time.time()
            if now < self._token_cooldown_until:
                raise RuntimeError(
                    f"goalserve inplay token cooldown {self._token_cooldown_until - now:.0f}s "
                    f"(last_error={self._last_token_error})"
                )
            self._last_token_attempt_at = now
            self._token_fetch_count += 1
            try:
                self._token, self._token_exp = await self._fetch_token()
            except Exception as exc:
                self._token_fetch_failures += 1
                self._last_token_error = repr(exc)
                self._token_cooldown_until = time.time() + _TOKEN_FAILURE_COOLDOWN_S
                self._persist_token_record()
                logger.warning(
                    "goalserve: inplay gettoken #%d FAILED: %s — cooldown %ds "
                    "(total fetches=%d failures=%d)",
                    self._token_fetch_count, exc, _TOKEN_FAILURE_COOLDOWN_S,
                    self._token_fetch_count, self._token_fetch_failures,
                )
                raise
            self._token_acquired_at = time.time()
            self._last_token_error = None
            self._persist_token_record()
            logger.info(
                "goalserve: inplay gettoken #%d OK, exp=%.0f — token slot occupied "
                "(total fetches=%d failures=%d)",
                self._token_fetch_count, self._token_exp,
                self._token_fetch_count, self._token_fetch_failures,
            )
            return self._token

    async def _ws_loop(self, sport: str) -> None:
        delay = _RECONNECT_BASE_S
        while True:
            try:
                token = await self._ensure_token()
                url = f"{_WS_BASE_URL}/{sport}?tkn={token}"
                logger.info("goalserve: connecting WS for %s", sport)
                # live.goalserve.com 需走代理；proxy=None 时 websockets 用默认行为。
                ws_kwargs = {"proxy": self._proxy} if self._proxy else {}
                async with websockets.connect(
                    url,
                    open_timeout=15,
                    ping_interval=30,
                    ping_timeout=10,
                    user_agent_header=_BROWSER_HEADERS["User-Agent"],
                    **ws_kwargs,
                ) as ws:
                    delay = _RECONNECT_BASE_S
                    self._consecutive_errors[sport] = 0
                    # 记录建连时间：重连后未被 avl 重新确认的旧 event 将被标记为孤立
                    self._reconnect_at[sport] = time.time()
                    logger.info("goalserve: WS connected for %s", sport)
                    async for raw in ws:
                        data = json.loads(raw)
                        await self._handle_message(sport, data)
            except asyncio.CancelledError:
                return
            except websockets.exceptions.InvalidStatus as exc:
                if exc.response.status_code == 401:
                    # Only clear if we're still holding the token that caused the 401.
                    # Without this check, all 8 sports handle 401 simultaneously and each
                    # clears the token after another sport already refreshed it, causing a
                    # cascade of gettoken calls that triggers 429 and fills the slot quota.
                    async with self._token_lock:
                        if self._token == token:
                            self._token = None
                            self._token_acquired_at = None
                            # 持久化清空后的记录（保留累计计数），不删文件——重启后
                            # 仍能看到历史 gettoken 次数与失败。
                            self._persist_token_record()
                    logger.warning("goalserve: WS 401 for %s, forcing token refresh", sport)
                elif exc.response.status_code == 403:
                    # 403 = 该运动 inplay 未授权（per-sport 订阅）。重试无用，
                    # 长退避避免每 60s 刷屏；若日后开通订阅会自动恢复。
                    err = self._consecutive_errors.get(sport, 0) + 1
                    self._consecutive_errors[sport] = err
                    logger.warning(
                        "goalserve: WS 403 for %s — sport not authorized for inplay; "
                        "backing off %ds (err#%d)",
                        sport, _SPORT_FORBIDDEN_BACKOFF_S, err,
                    )
                    await asyncio.sleep(_SPORT_FORBIDDEN_BACKOFF_S)
                    continue
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

        now = time.time()
        self._last_msg_time[sport] = now

        async with self._state_lock:
            if data.get("stp") == 99:
                self._state[sport].pop(event_id, None)
                self._state_seen_at[sport].pop(event_id, None)
                self._state_stp[sport].pop(event_id, None)
            else:
                stp = int(data.get("stp") or 0)
                self._state[sport][event_id] = data
                self._state_seen_at[sport][event_id] = now
                self._state_stp[sport][event_id] = stp

    def _evict_stale_events(self, sport: str, now: float) -> None:
        """驱逐不应继续占用内存的 event（在 _state_lock 内调用）。

        驱逐条件（任一满足）：
          1. 终态（stp∈{3,4,5}）且持有超过 _TERMINAL_HOLD_S → terminal_expired
          2. 重连后 _RECONNECT_GRACE_S 内未被 avl 重新确认 → reconnect_orphan
          3. 超过 _ORPHAN_TTL_S 未收到任何更新 → orphan_ttl
        """
        seen_at = self._state_seen_at.get(sport, {})
        stp_map = self._state_stp.get(sport, {})
        reconnect_at = self._reconnect_at.get(sport, 0.0)
        grace_elapsed = now - reconnect_at > _RECONNECT_GRACE_S

        to_evict: list[tuple[str, str]] = []
        for event_id, last_seen in seen_at.items():
            stp = stp_map.get(event_id, 0)
            age = now - last_seen

            if stp in _TERMINAL_STPS and age > _TERMINAL_HOLD_S:
                to_evict.append((event_id, "terminal_expired"))
            elif grace_elapsed and reconnect_at > 0 and last_seen < reconnect_at:
                # 建连后未被 avl 重新确认（last_seen 早于本次建连时间）
                to_evict.append((event_id, "reconnect_orphan"))
            elif age > _ORPHAN_TTL_S:
                to_evict.append((event_id, "orphan_ttl"))

        for event_id, reason in to_evict:
            self._state[sport].pop(event_id, None)
            self._state_seen_at[sport].pop(event_id, None)
            self._state_stp[sport].pop(event_id, None)
            logger.info("goalserve: evicted %s/%s reason=%s", sport, event_id, reason)

    def ws_per_sport_status(self) -> list[dict[str, Any]]:
        """每个 sport 的 WS 连接状态快照（不加锁，读瞬时值，仅用于观测）。

        首条为 inplay token 生命周期记录：暴露 token 占用、累计 gettoken 次数/失败、
        冷却剩余、当前活跃 WS 连接数——据此判断 token 槽占用与 WS 连接上限。
        """
        now = time.time()
        result: list[dict[str, Any]] = []
        connected = sum(
            1
            for sport in self._sports
            if self._consecutive_errors.get(sport, 0) == 0
            and self._last_msg_time.get(sport) is not None
            and now - self._last_msg_time[sport] <= _STALE_THRESHOLD_S
        )
        result.append({
            "sport": "_inplay_token",
            "type": "token",
            "token_present": self._token is not None,
            "token_exp_in_s": (
                round(self._token_exp - now, 1) if self._token is not None else None
            ),
            "token_age_s": (
                round(now - self._token_acquired_at, 1)
                if self._token_acquired_at is not None
                else None
            ),
            "gettoken_count": self._token_fetch_count,
            "gettoken_failures": self._token_fetch_failures,
            "cooldown_remaining_s": round(max(0.0, self._token_cooldown_until - now), 1),
            "last_error": self._last_token_error,
            "active_ws_connections": connected,
            "configured_sports": len(self._sports),
        })
        for sport in self._sports:
            last_msg = self._last_msg_time.get(sport)
            n_errors = self._consecutive_errors.get(sport, 0)
            events_count = len(self._state.get(sport, {}))
            reconnect_at = self._reconnect_at.get(sport)
            stale = last_msg is None or (now - last_msg) > _STALE_THRESHOLD_S
            result.append({
                "sport": sport,
                "type": "ws",
                "connected": n_errors == 0 and not stale,
                "consecutive_errors": n_errors,
                "events_in_memory": events_count,
                "last_msg_age_s": round(now - last_msg, 1) if last_msg is not None else None,
                "last_connected_at": reconnect_at,
            })
        return result

    async def list_events(self) -> SportsLiveSnapshot:
        await self._ensure_started()
        observed_at = utc_now(self._now_provider)
        now = time.time()

        async with self._state_lock:
            for sport in self._sports:
                self._evict_stale_events(sport, now)
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
