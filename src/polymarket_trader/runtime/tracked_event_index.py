"""TrackedEventIndex —— 已 match 的 source_event_id 反向索引 (R30 demand-driven).

parser 走 hot path 只解析 tracked event_ids, 未知 event 走 unknown_cap 兜底
(bootstrap 期间让 matcher 有机会 match), 解决 "未 match → 不在 tracked → parser
跳过 → matcher 永远拿不到 events" 死锁.

# 数据流
- 写者: LiveStateMatchService._apply_match (match 成功后 add)
- 读者: GoalserveInplayClient._fetch_once (parse 前 snapshot_for)
- 清理: lifecycle_binder._on_market_pruned (market prune 时 remove_for_market)

# 同场多盘口
一场比赛可能挂多个 condition (ML / Totals / Spreads), 共享同一 goalserve event_id.
prune 一个 condition 时检查是否还有其他 condition 共享, 共享则保留 sid.
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock


class TrackedEventIndex:
    """sport → frozenset[source_event_id] 反向索引."""

    def __init__(self) -> None:
        self._by_sport: dict[str, set[str]] = defaultdict(set)
        # condition_id → (sport, source_event_id) 反向索引, prune 时定向清理.
        self._by_condition: dict[str, tuple[str, str]] = {}
        self._lock = Lock()

    def add(self, sport: str, source_event_id: str, condition_id: str) -> None:
        """match 成功后写入. 空字符串静默跳过."""
        if not sport or not source_event_id or not condition_id:
            return
        with self._lock:
            self._by_sport[sport].add(source_event_id)
            self._by_condition[condition_id] = (sport, source_event_id)

    def remove_for_market(self, condition_id: str) -> None:
        """market prune 时调. 同场多盘口共享 sid 时保留."""
        with self._lock:
            entry = self._by_condition.pop(condition_id, None)
            if entry is None:
                return
            sport, sid = entry
            bucket = self._by_sport.get(sport)
            if bucket is None:
                return
            # 检查是否其他 condition 仍共享同 sid (同场多盘口).
            shared = any(s == sport and v == sid for s, v in self._by_condition.values())
            if not shared:
                bucket.discard(sid)
                if not bucket:
                    self._by_sport.pop(sport, None)

    def snapshot_for(self, sport: str) -> frozenset[str]:
        """parser 调用前拿 sport 的 tracked event_ids 不可变快照."""
        with self._lock:
            return frozenset(self._by_sport.get(sport, ()))

    def total_tracked(self) -> int:
        """observability 用."""
        with self._lock:
            return sum(len(v) for v in self._by_sport.values())


__all__ = ["TrackedEventIndex"]
