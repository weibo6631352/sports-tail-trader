"""时间窗口过滤的 domain 值对象。

只服务于只读查询接口（orders/fills/audit-events 等）的 ``since`` / ``until``
参数解析。Domain 层保持纯净：不依赖 FastAPI / SQLAlchemy / Settings；
epoch_ms 与 ``datetime`` 之间的转换在此集中，避免各路由/仓储/mixin
重复写转换 helper。

约定：``since`` 与 ``until`` 均为闭区间端点（>= since 且 <= until）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class TimeRange:
    """以毫秒 epoch 表达的可选时间窗口。

    任何字段为 ``None`` 表示该侧无界。``since_ms > until_ms`` 时构造即拒绝，
    不允许在调用链下游再触发不可达状态——上层（API 层）负责把校验失败
    映射为合适的错误响应。
    """

    since_ms: int | None = None
    until_ms: int | None = None

    def __post_init__(self) -> None:
        if (
            self.since_ms is not None
            and self.until_ms is not None
            and self.since_ms > self.until_ms
        ):
            raise ValueError("since_after_until")

    @property
    def is_empty(self) -> bool:
        """既无下界也无上界——调用方可借此短路过滤。"""

        return self.since_ms is None and self.until_ms is None

    def to_datetime_range(self) -> tuple[datetime | None, datetime | None]:
        """转换为 UTC ``datetime`` 元组，便于 SQL/内存比较使用。"""

        return (
            _epoch_ms_to_datetime(self.since_ms),
            _epoch_ms_to_datetime(self.until_ms),
        )

    def contains(self, moment: datetime | None) -> bool:
        """判断给定 UTC 时刻是否落在闭区间内。

        ``moment is None`` 视作未知时间——在任何有界窗口下都剔除，与 SQL
        中 ``NULL`` 永远不满足比较一致。
        """

        if self.is_empty:
            return True
        if moment is None:
            return False
        since_dt, until_dt = self.to_datetime_range()
        if since_dt is not None and moment < since_dt:
            return False
        if until_dt is not None and moment > until_dt:
            return False
        return True


def _epoch_ms_to_datetime(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
