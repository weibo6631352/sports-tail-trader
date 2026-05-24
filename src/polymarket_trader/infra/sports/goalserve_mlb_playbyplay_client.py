"""Goalserve MLB play-by-play 客户端（实时逐球事件流）。

数据源: https://www.goalserve.com/getfeed/{key}/baseball/mlb-playbyplay?json=1
更新频率: ~每球（实测 5-10s 轮询即可）
数据量: 全部 16 场 ~ 477KB total, ~30KB/场

输出结构 (per game_id):
  {
    "game_id": "1329200994",
    "status": "Final" / "In Progress" / etc.,
    "home_team": "Cincinnati Reds",
    "away_team": "St. Louis Cardinals",
    "home_score": int,
    "away_score": int,
    "innings": [
      {
        "period": "Top of 1" / "Bot of 9",
        "team": "awayteam" / "hometeam",
        "pitcher_description": "C. Paddack pitching for hometeam",
        "plays": [
          {
            "home_score": int,
            "away_score": int,
            "description": "Wetherholt grounded out to shortstop.",
            "pitches": [
              {"number": int, "result": "strike looking", "speed": int, "description": "Four-seam FB"},
              ...
            ],
          }
        ]
      }
    ],
    "fetched_at": ISO timestamp,
  }

仅观测 - 不参与决策（按 "先量化不决策" 原则）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_URL = "https://www.goalserve.com/getfeed/{key}/baseball/mlb-playbyplay?json=1"
_NBA_URL = "https://www.goalserve.com/getfeed/{key}/bsktbl/nba-playbyplay?json=1"
_DEFAULT_POLL_INTERVAL_S = 2.0  # 实时事件流：压最高频率（goalserve 未明示限速，per球级数据需要快）


@dataclass(slots=True)
class MlbPlayByPlayState:
    """单场 MLB 比赛的逐球状态快照（量化指标供分析）。"""

    game_id: str
    status: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    innings: list[dict[str, Any]]
    fetched_at: datetime
    server_clock_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "status": self.status,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_score": self.home_score,
            "away_score": self.away_score,
            "innings": self.innings,
            "innings_count": len(self.innings),
            "total_plays": sum(len(i.get("plays", [])) for i in self.innings),
            "total_pitches": sum(
                sum(len(p.get("pitches", [])) for p in i.get("plays", []))
                for i in self.innings
            ),
            "fetched_at": self.fetched_at.isoformat(),
            "server_clock_at": self.server_clock_at.isoformat() if self.server_clock_at else None,
        }


class MlbPlayByPlayClient:
    """MLB play-by-play HTTP 客户端（单 endpoint 全 16 场）。

    后台 task 每 8s 轮询一次，解析为 per-game state 写入内存 store。
    P0 主链路只读 store snapshot，不阻塞在 HTTP 上。
    """

    def __init__(
        self,
        *,
        api_key: str,
        url: str | None = None,
        proxy: str | None = None,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        timeout_s: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._url = (url or _DEFAULT_URL).format(key=api_key)
        self._poll_interval_s = max(1.5, float(poll_interval_s))  # 最低 1.5s 防过频
        self._states: dict[str, MlbPlayByPlayState] = {}
        self._last_fetched_at: datetime | None = None
        self._last_error: str | None = None
        self._fetch_count = 0
        self._error_count = 0
        if proxy:
            mounts = {
                "http://": httpx.AsyncHTTPTransport(proxy=proxy),
                "https://": httpx.AsyncHTTPTransport(proxy=proxy),
            }
            self._client = httpx.AsyncClient(mounts=mounts, timeout=timeout_s, trust_env=False)
        else:
            self._client = httpx.AsyncClient(timeout=timeout_s, trust_env=False)
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    def start(self) -> None:
        """启动后台轮询 task。"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop(), name="mlb_playbyplay_poll")

    async def aclose(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._client.aclose()

    def snapshot(self, game_id: str | None = None) -> dict[str, Any] | list[dict[str, Any]]:
        """读 store 当前快照（lock-free, GIL 保证）。"""
        if game_id:
            state = self._states.get(game_id)
            return state.as_dict() if state else {}
        return [s.as_dict() for s in self._states.values()]

    def status(self) -> dict[str, Any]:
        return {
            "tracked_games": len(self._states),
            "last_fetched_at": self._last_fetched_at.isoformat() if self._last_fetched_at else None,
            "last_error": self._last_error,
            "fetch_count": self._fetch_count,
            "error_count": self._error_count,
            "poll_interval_s": self._poll_interval_s,
        }

    async def _poll_loop(self) -> None:
        """后台轮询。

        休眠模式：如果上一次 fetch 0 个 "in progress" 场次（赛季外/比赛日空挡），
        延长间隔到 60s 节省 API 调用。一旦检测到有 active 场恢复正常 poll_interval_s。
        """
        idle_interval_s = 60.0
        while not self._stopped:
            try:
                await self._fetch_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error_count += 1
                self._last_error = str(exc)
                logger.warning("%s poll error: %s", type(self).__name__, exc)
            # demand-driven：判断当前是否有 active 场
            active_count = sum(
                1 for st in self._states.values()
                if (st.status or "").lower() in ("in progress", "live", "in play", "active")
            )
            interval = self._poll_interval_s if active_count > 0 else idle_interval_s
            await asyncio.sleep(interval)

    async def _fetch_once(self) -> None:
        import time as _time
        client_name = type(self).__name__
        _t0 = _time.perf_counter()
        try:
            r = await self._client.get(self._url)
        except Exception as exc:
            latency_ms = (_time.perf_counter() - _t0) * 1000
            self._error_count += 1
            self._last_error = str(exc)
            try:
                from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
                SystemPerfMonitor.get().record_api_call(
                    client_name, success=False, error_type=type(exc).__name__, latency_ms=latency_ms,
                )
            except Exception: pass
            return
        latency_ms = (_time.perf_counter() - _t0) * 1000
        self._fetch_count += 1
        if r.status_code != 200:
            self._last_error = f"http {r.status_code}"
            try:
                from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
                SystemPerfMonitor.get().record_api_call(
                    client_name, success=False,
                    error_type=f"HTTP_{r.status_code}", latency_ms=latency_ms,
                )
            except Exception: pass
            return
        # 解析 server Date
        server_clock_at: datetime | None = None
        date_hdr = r.headers.get("Date")
        if date_hdr:
            try:
                from email.utils import parsedate_to_datetime
                server_clock_at = parsedate_to_datetime(date_hdr)
            except Exception:
                pass
        try:
            data = r.json()
        except Exception as exc:
            self._last_error = f"json parse: {exc}"
            return
        cat = data.get("scores", {}).get("category")
        if isinstance(cat, list):
            cat = cat[0] if cat else None
        if not cat:
            return
        matches = cat.get("match") or []
        if isinstance(matches, dict):
            matches = [matches]
        now = datetime.now(timezone.utc)
        new_states: dict[str, MlbPlayByPlayState] = {}
        for m in matches:
            if not isinstance(m, dict):
                continue
            game_id = m.get("@id") or ""
            if not game_id:
                continue
            home = m.get("hometeam", {}) or {}
            away = m.get("awayteam", {}) or {}
            innings_raw = (m.get("playbyplay") or {}).get("inning") or []
            if isinstance(innings_raw, dict):
                innings_raw = [innings_raw]
            innings: list[dict[str, Any]] = []
            for inn in innings_raw:
                if not isinstance(inn, dict):
                    continue
                plays_raw = inn.get("play") or []
                if isinstance(plays_raw, dict):
                    plays_raw = [plays_raw]
                plays: list[dict[str, Any]] = []
                for p in plays_raw:
                    if not isinstance(p, dict):
                        continue
                    pitches_raw = (p.get("pitches") or {}).get("pitch") or []
                    if isinstance(pitches_raw, dict):
                        pitches_raw = [pitches_raw]
                    pitches = [
                        {
                            "number": _try_int(pt.get("@number")),
                            "result": pt.get("@result"),
                            "speed": _try_int(pt.get("@speed")),
                            "description": pt.get("@description"),
                        }
                        for pt in pitches_raw if isinstance(pt, dict)
                    ]
                    plays.append({
                        "home_score": _try_int(p.get("@homeScore")),
                        "away_score": _try_int(p.get("@awayScore")),
                        "description": p.get("@description"),
                        "pitches": pitches,
                    })
                innings.append({
                    "period": inn.get("@period"),
                    "team": inn.get("@team"),
                    "pitcher_description": inn.get("@description"),
                    "plays": plays,
                })
            new_states[game_id] = MlbPlayByPlayState(
                game_id=game_id,
                status=m.get("@status", "unknown"),
                home_team=home.get("@name", ""),
                away_team=away.get("@name", ""),
                home_score=_try_int(home.get("@totalscore")) or 0,
                away_score=_try_int(away.get("@totalscore")) or 0,
                innings=innings,
                fetched_at=now,
                server_clock_at=server_clock_at,
            )
        self._states = new_states
        self._last_fetched_at = now
        self._last_error = None
        try:
            from polymarket_trader.runtime.system_perf_monitor import SystemPerfMonitor
            mon = SystemPerfMonitor.get()
            mon.worker_tick(name=type(self).__name__, expected_interval_s=self._poll_interval_s)
            mon.record_api_call(type(self).__name__, success=True, latency_ms=latency_ms)
        except Exception:
            pass


def _try_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class NbaPlayByPlayClient(MlbPlayByPlayClient):
    """NBA 逐事件流（复用 MLB 模板，仅切换 URL）。

    NBA play-by-play 数据格式略不同——innings 替换为 quarter，play 字段差异。
    第一版直接复用 parser；后续按需细化。
    """

    def __init__(
        self,
        *,
        api_key: str,
        proxy: str | None = None,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        timeout_s: float = 15.0,
    ) -> None:
        super().__init__(
            api_key=api_key,
            url=_NBA_URL,
            proxy=proxy,
            poll_interval_s=poll_interval_s,
            timeout_s=timeout_s,
        )
