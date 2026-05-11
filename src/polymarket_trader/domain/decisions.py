"""Decision record 的 domain DTO。

录制 framework 调用 ``decide_entry`` / ``decide_exit`` / ``decide_follow_up``
等 hook 时的输入摘要与输出 decision，用于离线复盘与策略对比。

Domain 层保持纯净——不依赖 FastAPI / SQLAlchemy / py-clob-client；
context / decision payload 已在调用侧 ``jsonable`` 序列化为 mapping，
此处只承担稳定 schema 与不可变性约束。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_datetime(value: datetime | None) -> datetime:
    value = value or _utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """单次 hook 调用录制条目。

    - ``decision_input``：构造决策时的关键上下文摘要（市场、token、入参 metadata）。
    - ``decision_output``：策略 hook 返回的 decision 对象 jsonable 投影。
    - ``accepted``：是否产生了可执行 intent（用于 dump 端点 accepted=true/false 过滤）。
    - ``reason``：策略返回的拒绝/状态原因（必须复用已存在原因字符串，不创造新值）。
    """

    trace_id: str
    condition_id: str
    decision_input: Mapping[str, Any]
    decision_output: Mapping[str, Any]
    accepted: bool
    hook_name: str = ""
    token_id: str | None = None
    market_slug: str | None = None
    reason: str | None = None
    record_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _normalize_datetime(self.created_at))


__all__ = ["DecisionRecord"]
