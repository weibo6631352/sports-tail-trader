"""赛季状态同步 worker（P2，低频）。

与 ``SportsLiveStateWorker`` 兄弟：负责拉取赛季积分榜 / 系列赛分等长周期信号，
写入 ``SeasonStateStore``，并发出 ``SEASON_STATE_UPDATED`` 生命周期事件。
默认 cadence 1800s，远低于 live state worker 的 5s。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from polymarket_trader.domain.sports_season import (
    SeasonSnapshot,
    SeasonStateSyncStatus,
)
from polymarket_trader.extension_api.lifecycle import LifecycleEvent
from polymarket_trader.runtime.lifecycle_bus import LifecyclePublisher
from polymarket_trader.serialization import jsonable
from polymarket_trader.storage.season_state_store import SeasonStateStore

SeasonSnapshotProvider = Callable[[], Awaitable[SeasonSnapshot]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SportsSeasonStateWorker:
    """把外部赛季状态同步到 SeasonStateStore。"""

    priority = "P2"

    def __init__(
        self,
        *,
        snapshot_provider: SeasonSnapshotProvider,
        store: SeasonStateStore,
        lifecycle_bus: LifecyclePublisher | None = None,
        enabled: bool = False,
        source: str = "espn",
        leagues: tuple[str, ...] = (),
    ) -> None:
        self._snapshot_provider = snapshot_provider
        self._store = store
        self._lifecycle_bus = lifecycle_bus
        self._enabled = enabled
        self._source = source
        self._leagues = leagues
        self._running = False
        self._last_started_at: datetime | None = None
        self._last_completed_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._last_standings_count = 0
        self._last_series_count = 0

    async def sync_once(self) -> SeasonSnapshot | None:
        if not self._enabled:
            return None
        self._running = True
        self._last_started_at = _utc_now()
        self._last_error = None
        try:
            snapshot = await self._snapshot_provider()
        except Exception as exc:
            self._consecutive_failures += 1
            self._last_error = str(exc)
            self._last_completed_at = _utc_now()
            raise
        else:
            self._consecutive_failures = 0
            self._apply(snapshot)
            self._last_success_at = _utc_now()
            self._last_completed_at = self._last_success_at
            self._last_standings_count = len(snapshot.standings)
            self._last_series_count = len(snapshot.series)
            return snapshot
        finally:
            self._running = False

    def _apply(self, snapshot: SeasonSnapshot) -> None:
        for standings in snapshot.standings:
            self._store.upsert_standings(standings)
            if self._lifecycle_bus is not None:
                self._lifecycle_bus.publish(
                    LifecycleEvent.SEASON_STATE_UPDATED,
                    payload={
                        "kind": "standings",
                        "league": standings.league,
                        "season_id": standings.season_id,
                        "observed_at": standings.observed_at.isoformat(),
                        "row_count": len(standings.rows),
                        "source": standings.source,
                    },
                )
        for series in snapshot.series:
            self._store.upsert_series(series)
            if self._lifecycle_bus is not None:
                self._lifecycle_bus.publish(
                    LifecycleEvent.SEASON_STATE_UPDATED,
                    payload={
                        "kind": "series",
                        "league": series.league,
                        "series_id": series.series_id,
                        "observed_at": series.observed_at.isoformat() if series.observed_at else None,
                        "home_wins": series.home_wins,
                        "away_wins": series.away_wins,
                        "source": series.source,
                    },
                )

    def status_snapshot(self) -> SeasonStateSyncStatus:
        return SeasonStateSyncStatus(
            enabled=self._enabled,
            source=self._source,
            running=self._running,
            last_started_at=self._last_started_at,
            last_completed_at=self._last_completed_at,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            consecutive_failures=self._consecutive_failures,
            last_standings_count=self._last_standings_count,
            last_series_count=self._last_series_count,
            leagues=self._leagues,
        )

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self.status_snapshot())
