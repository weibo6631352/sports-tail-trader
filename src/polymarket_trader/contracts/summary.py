from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class StrategySummary:
    """策略主动提供给 framework 的中性展示视图。

    framework 的 admin / UI / audit / 复盘读这里的字段，不读策略私有 metadata。
    策略可把任何 framework 不解析、但 admin 详情页希望透传给前端的扩展结构放进
    ``extras``。framework 对 ``extras`` 整体序列化、不按字段名解释。
    """

    action: str = ""
    reason: str = ""
    label: str = ""
    market_type: str = ""
    side: str = ""
    line: Decimal | None = None
    best_ask: Decimal | None = None
    observed_at: datetime | None = None
    manual_confirmed: bool = False
    confirmed_by: str = ""
    confirm_reason: str = ""
    extras: Mapping[str, Any] = field(default_factory=dict)
