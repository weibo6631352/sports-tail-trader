"""SportsLiveHistoryBuffer —— SPORTS_LIVE_STATE_RECORDED 事件内存 ring buffer。

设计动机
========

``/sports/live-events`` 此前实现走 audit_events DB 查询（SportsQueryAggregator
→ TimelineAggregator）。前端 SportsEventsPage 虽是按需触发的 investigate
工具,但 §3 + 用户原则"能内存就内存"要求把首次窗口收敛到内存。

本 buffer 按 ``condition_id`` 分桶,每桶保留 ``maxlen_per_condition`` 条最近事件
（默认 200 条,按 deduper 30s 窗口约等于 100 分钟历史）。需要远期历史走
``/audit-events/by-condition/{cid}?channels=sports_live_state_recorded`` DB 路径。

数据流
======

``event_bus.broadcast`` → 本 buffer ``ingest_event`` listener → in-memory deque。
和 WS publisher 是平级的 broadcast listener,共存不冲突。

evict
=====

实现 ``MarketScopedStore`` 协议——``lifecycle_registry`` market lifecycle 结束时
按 condition_id evict。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.serialization import jsonable


_DEFAULT_MAXLEN_PER_CONDITION = 200


class SportsLiveHistoryBuffer:
    """asyncio 单线程模型——所有读写都在 event loop 同一线程,靠 GIL 原子性,无锁。
    对齐 ``OrderbookHistoryBuffer`` 的 "纯 dict + deque,无锁" 写法。
    """

    def __init__(self, *, maxlen_per_condition: int = _DEFAULT_MAXLEN_PER_CONDITION) -> None:
        self._maxlen = max(1, maxlen_per_condition)
        self._buckets: dict[str, deque[dict[str, Any]]] = {}

    # ---------- write side: event_bus broadcast listener ----------

    def ingest_event(self, event: Any) -> None:
        """``event_bus.add_broadcast_listener`` 回调——同步、必须快。

        非 SPORTS_LIVE_STATE_RECORDED 直接忽略；无 condition_id 也忽略
        （buffer 按 cid 分桶,无 cid 无法归属）。
        """
        event_type = getattr(event, "event_type", None)
        if event_type != DomainEventType.SPORTS_LIVE_STATE_RECORDED:
            return
        condition_id = getattr(event, "condition_id", None)
        if not condition_id:
            return
        entry = {
            "event_id": getattr(event, "event_id", None),
            "trace_id": getattr(event, "trace_id", None),
            "event_title": event_type.value if hasattr(event_type, "value") else str(event_type),
            "condition_id": condition_id,
            "token_id": getattr(event, "token_id", None),
            "market_slug": getattr(event, "market_slug", None),
            "created_at": jsonable(getattr(event, "created_at", None)),
            "payload": dict(getattr(event, "payload", {})),
        }
        bucket = self._buckets.get(condition_id)
        if bucket is None:
            bucket = deque(maxlen=self._maxlen)
            self._buckets[condition_id] = bucket
        bucket.appendleft(entry)

    # ---------- read side ----------

    def list_events(
        self,
        *,
        condition_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
        since_iso: str | None = None,
        until_iso: str | None = None,
    ) -> tuple[tuple[dict[str, Any], ...], int]:
        """按 cid（可选）+ 时间窗过滤,按 created_at 倒序返回 (items, total)。

        条目存储时已按 appendleft 维护倒序——直接遍历即可。
        """
        if condition_id:
            bucket = self._buckets.get(condition_id)
            items: list[dict[str, Any]] = list(bucket) if bucket else []
        else:
            # tuple(values()) 先快照桶引用,避免迭代中桶字典本身被改;每个桶是 deque,
            # list(bucket) 一次性拷出,后续过滤/排序在本地 list 完成。
            items = []
            for bucket in tuple(self._buckets.values()):
                items.extend(bucket)
        if since_iso is not None:
            items = [e for e in items if (e.get("created_at") or "") >= since_iso]
        if until_iso is not None:
            items = [e for e in items if (e.get("created_at") or "") <= until_iso]
        if not condition_id:
            items.sort(key=lambda e: e.get("created_at") or "", reverse=True)
        total = len(items)
        if offset:
            items = items[offset:]
        if limit:
            items = items[:limit]
        return tuple(items), total

    def tracked_condition_count(self) -> int:
        return len(self._buckets)

    # ---------- MarketScopedStore 协议 ----------

    def evict_market(self, condition_id: str, token_ids: tuple[str, ...]) -> None:
        del token_ids
        self._buckets.pop(condition_id, None)


__all__ = ["SportsLiveHistoryBuffer"]
