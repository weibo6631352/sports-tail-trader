"""family 级执行权限枚举（outright / series 子模块（已删））。"""

from __future__ import annotations

from enum import StrEnum


class ExecutionPermission(StrEnum):
    RECORD_ONLY = "record_only"
    ALERT_ONLY = "alert_only"
    MANUAL_CONFIRM = "manual_confirm"
    AUTO_EXECUTE = "auto_execute"
