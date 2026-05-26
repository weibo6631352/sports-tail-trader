"""OutboxAggregator —— outbox pending / failures 查询。

# 双数据源

- `list_pending` 走**进程内 runtime.outbox**（运行时 LocalOutbox），无 DB 调用
- `list_failures` 走 **DB**（崩溃后 DB 是唯一真相，内存丢）

# Endpoint 对应

| Endpoint | 方法 | 内容 |
|---|---|---|
| `GET /outbox/pending` | `list_pending(...)` | 进程内当前 pending 队列 |
| `GET /outbox/failures` | `list_failures(...)` | DB 中 retry_count > 0 / last_error 的事件 |
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from polymarket_trader.app.admin_serialization import AdminSerializer, page_payload
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db import RepositoryPage

from ._db import RepositoryGroup, with_repositories

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from polymarket_trader.infra.outbox.local_queue import LocalOutbox


class OutboxAggregator:
    def __init__(
        self,
        *,
        session_factory: "async_sessionmaker[AsyncSession] | None",
        runtime_outbox: "LocalOutbox | None" = None,
        serializer: AdminSerializer | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._runtime_outbox = runtime_outbox
        self._serializer = serializer or AdminSerializer()

    async def list_pending(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        if self._runtime_outbox is None:
            empty_page: RepositoryPage[Any] = RepositoryPage(
                items=tuple(), total=0, limit=limit, offset=offset
            )
            return page_payload(empty_page, serializer=self._serializer.outbox_event)
        events = self._runtime_outbox.pending_events()
        if trace_id is not None:
            events = tuple(e for e in events if e.trace_id == trace_id)
        page = RepositoryPage(
            items=tuple(events[offset : offset + limit]),
            total=len(events),
            limit=limit,
            offset=offset,
        )
        return page_payload(page, serializer=self._serializer.outbox_event)

    async def list_failures(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        event_type: str | None = None,
        time_range: TimeRange | None = None,
        min_retry_count: int = 1,
    ) -> dict[str, Any]:
        """DB 是唯一真相——崩溃后 pending_events 内存丢，必须从 DB 拉历史失败事件。"""

        if self._session_factory is None:
            empty_page: RepositoryPage[Any] = RepositoryPage(
                items=tuple(), total=0, limit=limit, offset=offset
            )
            return page_payload(empty_page, serializer=self._serializer.outbox_event)

        async def _query(repos: RepositoryGroup) -> RepositoryPage[Any]:
            return await repos.outbox.list_failures_snapshot(
                limit=limit,
                offset=offset,
                trace_id=trace_id,
                event_type=event_type,
                time_range=time_range,
                min_retry_count=min_retry_count,
            )

        page = await with_repositories(self._session_factory, _query)
        return page_payload(page, serializer=self._serializer.outbox_event)
