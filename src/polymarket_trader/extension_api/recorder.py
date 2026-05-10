"""决策录制契约。

framework 在调用 hook 时把"输入 context + 输出 decision"录到 ``DecisionRecorder``，
策略 / admin / 复盘工具基于录制重新跑新代码、对比新旧决策。

录制是异步副作用——抛错只走 telemetry，不阻塞主链路；
不写 DB（避免 schema 演化与 P0 路径耦合），实现侧默认在内存 ring buffer。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """单次 hook 调用的录制条目。

    ``context_payload`` / ``decision_payload`` 是 ``jsonable`` 后的字典；
    ReplayHarness 用 ``context_payload`` 重建 fixture，再让新 hooks 跑出新 decision，
    与 ``decision_payload`` 做 diff。
    """

    hook_name: str
    trace_id: str
    recorded_at: datetime
    context_payload: Mapping[str, Any]
    decision_payload: Mapping[str, Any]
    condition_id: str | None = None
    token_id: str | None = None
    market_slug: str | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)


class DecisionRecorder(Protocol):
    """framework 调用 hook 后投递 record。"""

    def record(self, record: DecisionRecord) -> None: ...

    def snapshot(self) -> tuple[DecisionRecord, ...]:
        """返回当前可读 records；用于 admin / CLI dump 使用。"""


def dump_records_to_jsonl(records: Iterable[DecisionRecord], path: Path) -> int:
    """把 records 离线写到 JSONL 文件。

    供 admin endpoint / 离线 CLI / 策略测试调用——策略包按 §6 不能 import
    ``polymarket_trader.runtime``，所以在 ``extension_api`` 暴露这个函数。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as fp:
        for record in records:
            payload = {
                "hook_name": record.hook_name,
                "trace_id": record.trace_id,
                "recorded_at": record.recorded_at.isoformat(),
                "condition_id": record.condition_id,
                "token_id": record.token_id,
                "market_slug": record.market_slug,
                "context_payload": record.context_payload,
                "decision_payload": record.decision_payload,
                "extras": record.extras,
            }
            fp.write(json.dumps(payload, default=str))
            fp.write("\n")
            written += 1
    return written
