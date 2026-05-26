"""策略层基础类型。

历史上承载完整扫尾评估器，现已删除。本模块只保留 outright / series 子策略仍
消费的 ``ExecutionPermission`` 枚举。
"""

from __future__ import annotations

from enum import StrEnum


class ExecutionPermission(StrEnum):
    """family 级执行权限——outright / series 子策略消费。"""

    RECORD_ONLY = "record_only"
    ALERT_ONLY = "alert_only"
    MANUAL_CONFIRM = "manual_confirm"
    AUTO_EXECUTE = "auto_execute"
