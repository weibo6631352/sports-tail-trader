"""Goalserve 赛前赔率同步 worker（P2，增量拉取）。

周期性调用 GoalservePregameOddsClient.fetch_all()，用 ts 增量减少流量；
snapshot 存内存供策略和 admin 读取，不直接写 EntryMetadataStore（市场匹配
由策略扩展按需查询 get_snapshots()）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from polymarket_trader.infra.sports.goalserve_pregame_client import (
    GoalservePregameOddsClient,
    GoalservePregameSnapshot,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GoalservePregameWorker:
    """周期性拉取 Goalserve 赛前赔率，供策略层按需查询。

    ts 增量：每次成功拉取后保存各运动的 ts，下次携带以减少数据量。
    单运动失败不影响其他运动（客户端内部已处理）。
    """

    priority = "P2"

    def __init__(
        self,
        *,
        client: GoalservePregameOddsClient,
        enabled: bool = False,
    ) -> None:
        self._client = client
        self._enabled = enabled
        self._snapshots: dict[str, GoalservePregameSnapshot] = {}
        self._ts_by_sport: dict[str, str] = {}
        self._last_started_at: datetime | None = None
        self._last_completed_at: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._last_sports_seen = 0
        self._last_matches_seen = 0

    async def sync_once(self) -> int:
        """拉取所有启用运动的赛前赔率；返回本次拉取到的 match 总数。"""

        if not self._enabled:
            return 0
        self._last_started_at = _utc_now()
        self._last_error = None
        try:
            snapshots = await self._client.fetch_all(ts_by_sport=self._ts_by_sport)
        except Exception as exc:
            self._consecutive_failures += 1
            self._last_error = str(exc)
            logger.warning("goalserve_pregame_worker: fetch_all failed: %s", exc)
            return 0

        self._consecutive_failures = 0
        total_matches = 0
        for sport, snapshot in snapshots.items():
            self._snapshots[sport] = snapshot
            if snapshot.ts:
                self._ts_by_sport[sport] = snapshot.ts
            total_matches += len(snapshot.matches)

        self._last_sports_seen = len(snapshots)
        self._last_matches_seen = total_matches
        self._last_completed_at = _utc_now()
        logger.debug(
            "goalserve_pregame_worker: sync done sports=%d matches=%d",
            len(snapshots),
            total_matches,
        )
        return total_matches

    def get_snapshots(self) -> dict[str, GoalservePregameSnapshot]:
        """返回最新赛前赔率快照（{sport: snapshot}），供策略层只读查询。"""

        return dict(self._snapshots)

    def get_snapshot(self, sport: str) -> GoalservePregameSnapshot | None:
        """按运动名返回快照；不存在返回 None。"""

        return self._snapshots.get(sport.lower().strip())

    def status_snapshot(self) -> Mapping[str, Any]:
        return {
            "enabled": self._enabled,
            "last_started_at": self._last_started_at.isoformat() if self._last_started_at else None,
            "last_completed_at": self._last_completed_at.isoformat() if self._last_completed_at else None,
            "last_error": self._last_error,
            "consecutive_failures": self._consecutive_failures,
            "sports_seen": self._last_sports_seen,
            "matches_seen": self._last_matches_seen,
            "sports_cached": list(self._snapshots.keys()),
        }
