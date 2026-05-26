"""TimelineAggregator —— audit 事件 / 复盘 / 时间线查询。

替代 `app/admin_query/timeline.py:AdminTimelineQueryMixin.list_audit_events`。
按 docs/新架构方案.md §12.2 审计查询类（走 DB）— 不缓存。

# Endpoint 对应

| Endpoint | 方法 | 内容 |
|---|---|---|
| `GET /audit-events` | `list_audit_events(...)` | 按 trace_id/event_title/condition_id 等过滤 |
| `GET /trades/timeline/{cid}` | `trade_timeline(cid)` | (待迁移) 单市场完整交易时间线 |

# 设计

aggregator 持 `session_factory` + `serializer`（共享 admin_serialization 复用，
不为了 §10 强行拆——serializer 是稳定 utility，多 aggregator 共享反而消除重复）。

`session_factory=None` 时 → 返回空 page（前端友好降级，不抛 500）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from polymarket_trader.app.admin_serialization import AdminSerializer, page_payload
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage

from ._db import RepositoryGroup, with_repositories

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class TimelineAggregator:
    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession] | None",
        serializer: AdminSerializer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._serializer = serializer or AdminSerializer()

    async def list_audit_events(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        event_title: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        time_range: TimeRange | None = None,
        with_total: bool = False,
    ) -> dict[str, Any]:
        if self._session_factory is None:
            empty_page: RepositoryPage[Any] = RepositoryPage(
                items=tuple(), total=0, limit=limit, offset=offset
            )
            return page_payload(empty_page, serializer=self._serializer.audit_event)

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.audit.list_audit_events_snapshot(
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                event_title=event_title,
                condition_id=condition_id,
                token_id=token_id,
                time_range=time_range,
                with_total=with_total,
            )

        page = await with_repositories(self._session_factory, _query)
        return page_payload(page, serializer=self._serializer.audit_event)
