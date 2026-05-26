"""AuditDeduper —— 内存 LRU dedupe，过滤/合并短窗口内重复 audit 事件。

原架构方案 §13.3 / §13.4。写入侧 dedupe：同 (event_type, condition_id,
payload_hash) 短窗口内首次 INSERT，重复触发 UPDATE 原始行的 occurrence_count + last_seen_at。
完全相同的 payload 已在 LRU 内 + 持久化层是同一行（避免 DB 膨胀），统计不丢。

# 设计

- **进程内单实例**：dict + monotonic 时间戳 + 简易 LRU eviction（容量上限）
- **分级 TTL 窗口**：按 event_type 分类设 TTL
  - 拒绝类（`*_failed` / `*_rejection_*`）：60s
  - heartbeat / SKIP 类：30s
  - record-only 评估：300s
  - 其他（默认）：10s
- **P3 不阻塞**：本类只内存操作，无 IO；PersistenceWorker 入口同步分流后再走 _persist_batch / _bump_occurrences
- **statistics 可观测**：`stats()` 返回 inserted / updated / total

# 用法

```python
deduper = AuditDeduper()
plan = deduper.classify(batch_events)
# plan.inserts → _persist_batch（首次见，常规写入）
# plan.updates → AuditEventRepository.bump_occurrences（已在 DB 里的行 +1）
```

# 与 GarbageFilter 互补

- `GarbageFilter` 判定"绝对不该写"（heartbeat SKIP 永不写）
- `AuditDeduper` 判定"短窗口内重复"（首次 insert，后续 update 累计）

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
    inserted: int = 0
    updated: int = 0
    by_event_type_updated: dict[str, int] = field(default_factory=dict)

    def as_payload(self) -> dict[str, int | dict[str, int]]:
        return {
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "by_event_type_updated": dict(self.by_event_type_updated),
        }


@dataclass(frozen=True, slots=True)
class OccurrenceBump:
    """命中已有行的更新指令——worker 据此发 UPDATE。"""

    target_event_id: str  # 原始首次写入的 event_id（用于 WHERE 定位 audit_events 行）
    event_type: str       # 仅统计用
    last_seen_at: float   # unix epoch seconds


@dataclass(frozen=True, slots=True)
class DedupePlan:
    inserts: tuple = ()
    updates: tuple[OccurrenceBump, ...] = ()


def payload_hash(payload) -> str:
    """计算 payload 的稳定 hash（按 sorted keys JSON 序列化）。

    blake2b-8 hex (16 char)。供 deduper 内存 LRU + DB audit_events.payload_hash 字段共用，
    保证内存判定与 DB 写入对同一 payload 用相同 hash。
    """

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


@dataclass(slots=True)
class _SeenEntry:
    """LRU map 的 value：记录首次事件 event_id + 过期时间。"""

    target_event_id: str  # 首次入库时的 event_id（UPDATE 路径用）
    expiry_monotonic: float


class AuditDeduper:
    def __init__(
        self,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._max_entries = max_entries
        # OrderedDict 保留插入顺序 → 用作简易 LRU（容量满时 popitem(last=False) 踢最旧）
        self._seen: OrderedDict[tuple[str, str | None, str], _SeenEntry] = OrderedDict()
        self._stats = DedupeStats()

    def classify(self, events: Iterable[OutboxEvent]) -> DedupePlan:
        """分流批量事件为首次写入 + 累计更新两路。

        - INSERT：dedupe 窗口内首次见 / 已过期 → 走常规 _persist_batch
        - UPDATE：命中窗口内已有 event_id → 发 SQL UPDATE 原始行 occurrence_count + last_seen_at
        """

        inserts: list[OutboxEvent] = []
        updates: list[OccurrenceBump] = []
        now_mono = time.monotonic()
        now_epoch = time.time()
        for event in events:
            self._stats.total += 1
            key = (event.event_type, event.condition_id, payload_hash(event.payload))
            ttl_s = _ttl_for_event_type(event.event_type)
            existing = self._seen.get(key)
            if existing is not None and existing.expiry_monotonic > now_mono:
                # 窗口内重复 → UPDATE 路径
                self._seen.move_to_end(key)
                updates.append(
                    OccurrenceBump(
                        target_event_id=existing.target_event_id,
                        event_type=event.event_type,
                        last_seen_at=now_epoch,
                    )
                )
                self._stats.updated += 1
                self._stats.by_event_type_updated[event.event_type] = (
                    self._stats.by_event_type_updated.get(event.event_type, 0) + 1
                )
                continue

            # 首次见 / 已过期 → INSERT 路径 + 记录首次 event_id
            self._seen[key] = _SeenEntry(
                target_event_id=event.event_id,
                expiry_monotonic=now_mono + ttl_s,
            )
            self._seen.move_to_end(key)
            while len(self._seen) > self._max_entries:
                self._seen.popitem(last=False)
            inserts.append(event)
            self._stats.inserted += 1

        return DedupePlan(inserts=tuple(inserts), updates=tuple(updates))

    def stats(self) -> DedupeStats:
        return DedupeStats(
            total=self._stats.total,
            inserted=self._stats.inserted,
            updated=self._stats.updated,
            by_event_type_updated=dict(self._stats.by_event_type_updated),
        )

    def reset_stats(self) -> None:
        self._stats = DedupeStats()
