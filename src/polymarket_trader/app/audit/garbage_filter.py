"""GarbageFilter —— 判定 audit event 是否"垃圾"（绝对不该写入）。

docs/新架构方案.md §13.2。`AuditDeduper` 处理"短窗口内重复"；GarbageFilter
处理"永远不该写"——两者互补。

# 6 类垃圾（§13.2）

| 类型 | 例子 | 处理 |
|---|---|---|
| **synthetic heartbeat SKIP** | `position_heartbeat` 触发 decide_exit 返回 SKIP | DROP |
| **record-only 反复评估** | 明确 `record_only=True` 的 market 每 tick 评估 | DROP（除非状态首次变化）|
| **payload 完全空** | 无 trace_id / payload / reason 全空 | DROP |
| **trivial counter** | discovery_tick_completed 等纯计数事件 | DROP（用 metric 替代）|
| **payload 超大** | > 2KB 的 dump-style 事件（§13.7 禁止）| DROP + warn |
| **debug log 性质** | event_type 含 "debug_" 前缀 | DROP |

# 与 AuditDeduper 顺序

PersistenceWorker 入口：先 GarbageFilter（永不写）→ 再 AuditDeduper（去重），
后者只看 GarbageFilter 留下的"值得写"事件。

# 阈值

`_PAYLOAD_SIZE_WARN_BYTES`、`_TRIVIAL_EVENT_TYPES` 等常量在本模块顶端，按
实际数据观察调整。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from polymarket_trader.domain.events import OutboxEvent

logger = logging.getLogger(__name__)


# § 13.7 payload size 上限。超大直接 drop + warn（说明业务路径塞了大 dump）
_PAYLOAD_SIZE_WARN_BYTES: int = 2048

# 已知 trivial counter event_type（应用 MetricsRegistry 而非 audit）
_TRIVIAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        # 占位——后续按数据观察补
    }
)

# 已知 synthetic heartbeat 来源的 payload source 标识
_HEARTBEAT_SOURCE_VALUES: frozenset[str] = frozenset({"position_heartbeat"})


@dataclass(slots=True)
class GarbageFilterStats:
    total: int = 0
    kept: int = 0
    dropped: int = 0
    by_reason_dropped: dict[str, int] = field(default_factory=dict)

    def as_payload(self) -> dict[str, int | dict[str, int]]:
        return {
            "total": self.total,
            "kept": self.kept,
            "dropped": self.dropped,
            "by_reason_dropped": dict(self.by_reason_dropped),
        }


def _payload_size_bytes(event: OutboxEvent) -> int:
    if not event.payload:
        return 0
    try:
        return len(json.dumps(event.payload, default=str))
    except (TypeError, ValueError):
        return len(repr(event.payload))


def _is_synthetic_heartbeat_skip(event: OutboxEvent) -> bool:
    """`position_heartbeat` 触发的 SKIP 决策（无任何动作）—— 不值得写。

    判定：
    - payload.source ∈ _HEARTBEAT_SOURCE_VALUES
    - reason 或 payload.action 表明是 SKIP（无 BUY / SELL 决策）
    """

    payload = event.payload or {}
    source = str(payload.get("source", ""))
    if source not in _HEARTBEAT_SOURCE_VALUES:
        return False
    # 判定是否 SKIP：action == "skip"（含 keep / no_op 等同义）或 reason 提示无动作
    action = str(payload.get("action", "")).lower()
    if action in {"skip", "keep", "no_op", "noop"}:
        return True
    return False


def _is_debug_event(event_type: str) -> bool:
    return event_type.startswith("debug_")


class GarbageFilter:
    def __init__(self) -> None:
        self._stats = GarbageFilterStats()

    def filter(self, events: Iterable[OutboxEvent]) -> tuple[OutboxEvent, ...]:
        kept: list[OutboxEvent] = []
        for event in events:
            self._stats.total += 1
            verdict = self.classify(event)
            if verdict is None:
                self._stats.kept += 1
                kept.append(event)
            else:
                self._stats.dropped += 1
                self._stats.by_reason_dropped[verdict] = (
                    self._stats.by_reason_dropped.get(verdict, 0) + 1
                )
                if verdict == "payload_too_large":
                    logger.warning(
                        "audit_event payload too large, dropped: event_type=%s size=%d",
                        event.event_type,
                        _payload_size_bytes(event),
                    )
        return tuple(kept)

    def classify(self, event: OutboxEvent) -> str | None:
        """返回 drop reason；None 表示保留。

        判定顺序：低成本检查在前（避免序列化大 payload 后再判定）。
        """

        if _is_debug_event(event.event_type):
            return "debug_event"
        if event.event_type in _TRIVIAL_EVENT_TYPES:
            return "trivial_counter"
        if _is_synthetic_heartbeat_skip(event):
            return "synthetic_heartbeat_skip"
        # 大 payload 检查放最后（最贵）
        if event.payload and _payload_size_bytes(event) > _PAYLOAD_SIZE_WARN_BYTES:
            return "payload_too_large"
        return None

    def stats(self) -> GarbageFilterStats:
        return GarbageFilterStats(
            total=self._stats.total,
            kept=self._stats.kept,
            dropped=self._stats.dropped,
            by_reason_dropped=dict(self._stats.by_reason_dropped),
        )

    def reset_stats(self) -> None:
        self._stats = GarbageFilterStats()
