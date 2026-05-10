"""DecisionRecorder 的 framework 内置实现。

默认是 in-memory ring buffer：保留最近 N 条决策，O(1) 写入，无 IO。
admin / CLI 可以把 buffer dump 成 JSONL 离线分析；策略二次开发可以在测试 fixture
里用 ``InMemoryDecisionRecorder`` 断言"我的 hook 在某 context 下输出了什么"。
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from polymarket_trader.extension_api.recorder import (
    DecisionRecord,
    DecisionRecorder,
    dump_records_to_jsonl as _dump_to_jsonl,
)
from polymarket_trader.serialization import jsonable

_logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryDecisionRecorder(DecisionRecorder):
    """线程安全 ring buffer。"""

    def __init__(self, *, capacity: int = 200) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._buffer: deque[DecisionRecord] = deque(maxlen=capacity)
        self._lock = Lock()

    def record(self, record: DecisionRecord) -> None:
        with self._lock:
            self._buffer.append(record)

    def snapshot(self) -> tuple[DecisionRecord, ...]:
        with self._lock:
            return tuple(self._buffer)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


def build_decision_record(
    *,
    hook_name: str,
    trace_id: str,
    context: Any,
    decision: Any,
    condition_id: str | None = None,
    token_id: str | None = None,
    market_slug: str | None = None,
    extras: Mapping[str, Any] | None = None,
) -> DecisionRecord:
    """把任意 dataclass / dict 转成 ``DecisionRecord``。

    序列化失败时返回带 error 标记的 record，而不是抛出，确保录制不阻塞主链路。
    """

    try:
        context_payload = jsonable(context)
    except Exception as exc:
        _logger.exception("failed to serialize context for recorder; trace_id=%s", trace_id)
        context_payload = {"_serialize_error": str(exc)}
    try:
        decision_payload = jsonable(decision)
    except Exception as exc:
        _logger.exception("failed to serialize decision for recorder; trace_id=%s", trace_id)
        decision_payload = {"_serialize_error": str(exc)}
    return DecisionRecord(
        hook_name=hook_name,
        trace_id=trace_id,
        recorded_at=_utc_now(),
        context_payload=context_payload if isinstance(context_payload, Mapping) else {"value": context_payload},
        decision_payload=decision_payload if isinstance(decision_payload, Mapping) else {"value": decision_payload},
        condition_id=condition_id,
        token_id=token_id,
        market_slug=market_slug,
        extras=dict(extras or {}),
    )


def dump_records_to_jsonl(records: tuple[DecisionRecord, ...], path: Path) -> int:
    """把 records 离线写到 JSONL。委托给 ``extension_api.recorder.dump_records_to_jsonl``，
    runtime 层保留 helper 仅为兼容已有 import 路径。"""

    return _dump_to_jsonl(records, path)
