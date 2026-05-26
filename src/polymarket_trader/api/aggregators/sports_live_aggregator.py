"""SportsLiveAggregator —— 直播源状态聚合（基于 LiveStateStore + LiveSourceRegistry）。

直接读 LiveStateStore.all_buckets + LiveSourceRegistry.summary，无需跨
metadata_store + worker 拼接。聚焦 V2 直播源架构（per-source bucket + 订阅注册）；
旧版基于 market_metadata_store 的 sports 查询在 `SportsQueryAggregator`。

# Endpoint 对应

| Endpoint | 方法 | 内容 |
|---|---|---|
| `GET /sports/live_states` | `live_states(level=summary or detail)` | 所有 bucket events + subscribers |
| `GET /sports/live_source_gaps` | `live_source_gaps()` | 应该有数据但 bucket 空/stale 的 sport |
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from polymarket_trader.pipeline.ingest.live_source import (
        LiveSourceRegistry,
        LiveStateStore,
    )

Level = Literal["summary", "detail"]

# bucket stale 阈值（与 observability.health 一致）
_STALE_THRESHOLD_S: float = 60.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SportsLiveAggregator:
    def __init__(
        self,
        *,
        live_state_store: "LiveStateStore",
        live_source_registry: "LiveSourceRegistry",
    ) -> None:
        self._store = live_state_store
        self._registry = live_source_registry

    def live_states(self, *, level: Level = "summary") -> dict[str, Any]:
        now = _utc_now()
        buckets = []
        for bucket in self._store.all_buckets():
            stale_s = (now - bucket.observed_at).total_seconds()
            subs = self._registry.subscribers_for(bucket.source)
            entry = {
                "source": bucket.source.as_label(),
                "provider": bucket.source.provider.value,
                "sport": bucket.source.sport,
                "events_count": len(bucket.events),
                "observed_at": bucket.observed_at.isoformat(),
                "stale_seconds": stale_s,
                "is_stale": stale_s > _STALE_THRESHOLD_S,
                "health": bucket.health.value,
                "subscriber_count": len(subs),
            }
            if level == "detail":
                entry["events"] = tuple(self._serialize_event(e) for e in bucket.events)
                entry["subscribers"] = sorted(subs)
                entry["last_error"] = bucket.last_error
            buckets.append(entry)
        return {
            "buckets": tuple(buckets),
            "subscriptions": self._registry.summary(),
        }

    def live_source_gaps(self) -> dict[str, Any]:
        """有 subscriber 但 bucket 空 / stale —— 直播数据漏抓的 sport 列表。

        与 §11.4 health/live_sources 互补：health 提供 status；本方法提供详细
        gap 列表给运维查证（CLAUDE.md §18 "无交易机会时必须做差异核对"）。
        """

        now = _utc_now()
        gaps = []
        for source in self._registry.active_sources():
            subs = self._registry.subscribers_for(source)
            if not subs:
                continue
            bucket = self._store.bucket(source)
            if bucket is None:
                gaps.append(
                    {
                        "source": source.as_label(),
                        "subscriber_count": len(subs),
                        "gap_type": "no_bucket",
                        "stale_seconds": None,
                    }
                )
                continue
            stale_s = (now - bucket.observed_at).total_seconds()
            if not bucket.events:
                gap_type = "empty_bucket"
            elif stale_s > _STALE_THRESHOLD_S:
                gap_type = "stale_bucket"
            else:
                continue  # healthy
            gaps.append(
                {
                    "source": source.as_label(),
                    "subscriber_count": len(subs),
                    "gap_type": gap_type,
                    "stale_seconds": stale_s,
                    "events_count": len(bucket.events),
                    "health": bucket.health.value,
                    "last_error": bucket.last_error,
                }
            )
        return {
            "stale_threshold_s": _STALE_THRESHOLD_S,
            "gaps": tuple(gaps),
        }

    def _serialize_event(self, event) -> dict[str, Any]:
        return {
            "source_event_id": event.source_event_id,
            "kind": event.kind.value,
            "league": event.league,
            "sport": event.sport,
            "status": event.status.value,
            "period": event.period,
            "seconds_remaining": event.seconds_remaining,
            "event_name": event.event_name,
            "event_start_time": (
                event.event_start_time.isoformat() if event.event_start_time else None
            ),
            "home_name": event.home.name if event.home is not None else None,
            "away_name": event.away.name if event.away is not None else None,
            "home_score": event.home.score if event.home is not None else None,
            "away_score": event.away.score if event.away is not None else None,
        }
