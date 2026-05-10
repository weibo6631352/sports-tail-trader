"""人工确认凭证。

admin 触发候选确认时，framework 把这个 DTO 注入 ``ExtensionContext.manual_confirmation``。
策略读它而不是策略私有 metadata key——admin / framework 与策略之间不再用字符串
约定通信。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ManualConfirmation:
    """单次人工确认的可审计凭证。"""

    operator: str
    reason: str = "manual_confirm"
    confirmed_at: datetime | None = None
