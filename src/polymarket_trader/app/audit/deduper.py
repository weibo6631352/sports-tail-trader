"""AuditDeduper —— 内存 LRU dedupe，过滤短窗口内重复 audit 事件。

docs/新架构方案.md §13.3。写入侧 dedupe：同 (event_type, condition_id,
payload_hash) 短窗口内只入库 1 次，命中重复时丢掉（不阻塞主路径）。

# 设计

- **进程内单实例**：dict + monotonic 时间戳 + 简易 LRU eviction（容量上限）
- **分级 TTL 窗口**：按 event_type 分类设 TTL（§13.3 表）
  - 拒绝类（`*_failed` / `*_rejection_*`）：60s
  - heartbeat 类 / SKIP 类：30s
  - record-only 评估：300s
  - 其他（默认）：10s
- **PR3 不阻塞**：本类只内存操作，无 IO；PersistenceWorker 入口同步过滤后再走 _persist_batch
- **统计可观测**：`stats()` 返回 kept / dropped / total 等供 operator / metric 查

# 用法

```python
deduper = AuditDeduper()
kept_events = deduper.filter(batch_events)  # 返回去重后子集
# kept_events 走 _persist_batch；被 drop 的仅记入 stats
```

# 与 GarbageFilter 互补

- `GarbageFilter` 判定"绝对不该写"（heartbeat SKIP 永不写）
- `AuditDeduper` 判定"短窗口内重复"（首次写、后续丢）

PersistenceWorker 入口先 GarbageFilter 再 AuditDeduper。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field

from polymarket_trader.domain.events import OutboxEvent

logger = logging.getLogger(__name__)

# event_type 前缀 → TTL（s）。匹配按前缀，首匹配优先。
_DEFAULT_TTL_RULES: tuple[tuple[str, float], ...] = (
    # 拒绝类（高频）
    ("risk_check_failed", 60.0),
    ("risk_rejection", 60.0),
    ("order_rejected", 60.0),
    ("allocation_decision_recorded", 60.0),  # 含 rejected 决策
    # heartbeat / SKIP 类
    ("sports_live_state_recorded", 30.0),  # state hash 相同时高频
    # record-only / 长 cadence 评估类
    ("market_filtered_out", 300.0),
    ("decision_recorded", 30.0),
    # 其他默认
)
_DEFAULT_TTL_S: float = 10.0

_DEFAULT_MAX_ENTRIES: int = 10000


@dataclass(slots=True)
class DedupeStats:
    total: int = 0
    kept: int = 0
    dropped: int = 0
    by_event_type_dropped: dict[str, int] = field(default_factory=dict)

    def as_payload(self) -> dict[str, int | dict[str, int]]:
        return {
            "total": self.total,
            "kept": self.kept,
            "dropped": self.dropped,
            "by_event_type_dropped": dict(self.by_event_type_dropped),
        }


def _payload_hash(payload) -> str:
    """计算 payload 的稳定 hash（按 sorted keys JSON 序列化）。"""

    if not payload:
        return "0"
    try:
        canonical = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        # 含不可序列化对象 → 用 repr 兜底（不稳定但能过 hash）
        canonical = repr(payload)
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()


def _ttl_for_event_type(event_type: str) -> float:
    for prefix, ttl in _DEFAULT_TTL_RULES:
        if event_type.startswith(prefix):
            return ttl
    return _DEFAULT_TTL_S


class AuditDeduper:
    def __init__(
        self,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._max_entries = max_entries
        # OrderedDict 保留插入顺序 → 用作简易 LRU（容量满时 popitem(last=False) 踢最旧）
        self._seen: OrderedDict[tuple[str, str | None, str], float] = OrderedDict()
        self._stats = DedupeStats()

    def filter(self, events: Iterable[OutboxEvent]) -> tuple[OutboxEvent, ...]:
        """返回去重后子集；命中重复的事件被 drop（仅记 stats）。"""

        kept: list[OutboxEvent] = []
        for event in events:
            self._stats.total += 1
            if self.should_keep(event):
                self._stats.kept += 1
                kept.append(event)
            else:
                self._stats.dropped += 1
                self._stats.by_event_type_dropped[event.event_type] = (
                    self._stats.by_event_type_dropped.get(event.event_type, 0) + 1
                )
        return tuple(kept)

    def should_keep(self, event: OutboxEvent) -> bool:
        """单事件检查：True 表示首次见 / 上次入库已超 TTL，应保留。"""

        key = (event.event_type, event.condition_id, _payload_hash(event.payload))
        now = time.monotonic()
        ttl_s = _ttl_for_event_type(event.event_type)

        existing = self._seen.get(key)
        if existing is not None and existing > now:
            # 仍在 TTL 窗口内 → drop
            self._seen.move_to_end(key)  # 维护 LRU 顺序
            return False

        # 首次见 / 已过期 → 标记 + 保留
        self._seen[key] = now + ttl_s
        self._seen.move_to_end(key)
        # 容量上限——踢最旧
        while len(self._seen) > self._max_entries:
            self._seen.popitem(last=False)
        return True

    def stats(self) -> DedupeStats:
        return DedupeStats(
            total=self._stats.total,
            kept=self._stats.kept,
            dropped=self._stats.dropped,
            by_event_type_dropped=dict(self._stats.by_event_type_dropped),
        )

    def reset_stats(self) -> None:
        self._stats = DedupeStats()
