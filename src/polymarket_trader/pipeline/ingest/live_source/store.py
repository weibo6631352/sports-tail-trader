"""LiveStateStore —— 按 LiveSourceKey 分桶的最新 events 存储。

Feeder 周期性把 `(provider, sport)` 维度的 events 推到对应 bucket；
`LiveStateMatchService` 注册 refresh listener，bucket 更新时同步触发 match + calibrate。

# 同步 listener 设计

`update()` 内 fan-out 是**同步**的——listener 必须是快速操作（matcher / calibrator
单次几 ms）。listener 异常被吞 + 落日志，不影响其他 listener 和 feeder 主循环。

# 不存全历史

每个 source 只保留**最新一次** snapshot。直播比分实时性要求只看当前；如果历史
数据有用（debug / replay），从 audit_events 拉。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from polymarket_trader.domain.sports_live import LiveEvent, SportsLiveSourceHealth

from .source import LiveSourceKey

logger = logging.getLogger(__name__)

RefreshListener = Callable[[LiveSourceKey], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class LiveSourceBucket:
    """单个 LiveSourceKey 最新一次 fetch 的状态快照。"""

    source: LiveSourceKey
    events: tuple[LiveEvent, ...]
    observed_at: datetime
    health: SportsLiveSourceHealth
    last_error: str | None = None


class LiveStateStore:
    def __init__(self) -> None:
        self._buckets: dict[LiveSourceKey, LiveSourceBucket] = {}
        self._listeners: list[RefreshListener] = []
        self._lock = Lock()

    def update(
        self,
        source: LiveSourceKey,
        events: tuple[LiveEvent, ...],
        *,
        observed_at: datetime | None = None,
        health: SportsLiveSourceHealth = SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA,
        last_error: str | None = None,
    ) -> None:
        bucket = LiveSourceBucket(
            source=source,
            events=tuple(events),
            observed_at=observed_at or _utc_now(),
            health=health,
            last_error=last_error,
        )
        with self._lock:
            self._buckets[source] = bucket
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(source)
            except Exception:  # noqa: BLE001 — listener 故障不可拖死 feeder 主循环
                logger.exception(
                    "LiveStateStore refresh listener failed for source=%s",
                    source.as_label(),
                )

    def bucket(self, source: LiveSourceKey) -> LiveSourceBucket | None:
        with self._lock:
            return self._buckets.get(source)

    def events_for(self, source: LiveSourceKey) -> tuple[LiveEvent, ...]:
        bucket = self.bucket(source)
        return bucket.events if bucket else ()

    def all_buckets(self) -> tuple[LiveSourceBucket, ...]:
        with self._lock:
            return tuple(self._buckets.values())

    def stale_age_s(
        self, source: LiveSourceKey, *, reference: datetime | None = None
    ) -> float | None:
        bucket = self.bucket(source)
        if bucket is None:
            return None
        ref = reference or _utc_now()
        return (ref - bucket.observed_at).total_seconds()

    def evict(self, source: LiveSourceKey) -> None:
        """source 无 subscriber 时清空 bucket——避免陈旧数据骗 matcher。"""

        with self._lock:
            self._buckets.pop(source, None)

    def register_refresh_listener(self, listener: RefreshListener) -> None:
        with self._lock:
            self._listeners.append(listener)
