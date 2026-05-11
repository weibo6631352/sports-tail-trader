from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy import or_, select

from polymarket_trader.domain.events import OutboxEvent
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db.models import OutboxEventModel
from polymarket_trader.infra.db.repositories._base import (
    BaseRepository,
    RepositoryPage,
    _limit_offset,
    _row_dict,
)


class OutboxEventRepository(BaseRepository):
    """本地 outbox 仓储。"""

    async def save_event(self, event: OutboxEvent, *, raw_payload: dict[str, Any] | None = None) -> OutboxEvent:
        await self.save_events([event], raw_payloads=[raw_payload])
        return event

    async def save_events(
        self,
        events: Iterable[OutboxEvent],
        *,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        events = tuple(events)
        payloads = raw_payloads or (None,) * len(events)
        rows = [
            _row_dict(OutboxEventModel.from_domain(event, raw_payload=payload))
            for event, payload in zip(events, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            OutboxEventModel,
            rows,
            conflict_columns=("idempotency_key",),
            update_columns=(
                "event_id",
                "trace_id",
                "event_type",
                "market_slug",
                "event_slug",
                "condition_id",
                "token_id",
                "reason",
                "priority",
                "retry_count",
                "last_error",
                "raw_response_summary",
                "payload",
                "updated_at",
            ),
        )

    async def list_pending_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
    ) -> RepositoryPage[OutboxEvent]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(OutboxEventModel).order_by(
            OutboxEventModel.priority.asc(),
            OutboxEventModel.created_at.asc(),
            OutboxEventModel.id.asc(),
        )
        if trace_id is not None:
            stmt = stmt.where(OutboxEventModel.trace_id == trace_id)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(
            items=tuple(row.to_domain() for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )

    async def list_events_by_types_snapshot(
        self,
        *,
        event_types: Sequence[str],
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        condition_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> RepositoryPage[OutboxEvent]:
        """按 ``event_type`` 白名单分页查询 outbox 事件。

        outbox_events 表是 append-only 审计层：lifecycle / reconcile / fill 等事件
        都会先落 outbox 再被消费，本接口给上层做"按事件类型回放"用，按
        ``created_at`` 倒序。
        """

        limit, offset = _limit_offset(limit, offset)
        types = tuple(event_types or ())
        if not types:
            return RepositoryPage(items=tuple(), total=0, limit=limit, offset=offset)
        stmt = (
            select(OutboxEventModel)
            .where(OutboxEventModel.event_type.in_(types))
            .order_by(
                OutboxEventModel.created_at.desc(),
                OutboxEventModel.id.desc(),
            )
        )
        if trace_id is not None:
            stmt = stmt.where(OutboxEventModel.trace_id == trace_id)
        if condition_id is not None:
            stmt = stmt.where(OutboxEventModel.condition_id == condition_id)
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(OutboxEventModel.created_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(OutboxEventModel.created_at <= until_dt)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(
            items=tuple(row.to_domain() for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )

    async def list_failures_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        event_type: str | None = None,
        time_range: TimeRange | None = None,
        min_retry_count: int = 1,
    ) -> RepositoryPage[OutboxEvent]:
        """Outbox 失败/重试事件视图。

        与 ``list_pending_snapshot`` 互补：这里只回 ``retry_count >= min_retry_count``
        或 ``last_error IS NOT NULL`` 的事件，按 ``updated_at`` 倒序，用于诊断
        持久化链路的问题。
        """

        limit, offset = _limit_offset(limit, offset)
        stmt = (
            select(OutboxEventModel)
            .where(
                or_(
                    OutboxEventModel.retry_count >= max(min_retry_count, 0),
                    OutboxEventModel.last_error.isnot(None),
                )
            )
            .order_by(
                OutboxEventModel.updated_at.desc(),
                OutboxEventModel.id.desc(),
            )
        )
        if trace_id is not None:
            stmt = stmt.where(OutboxEventModel.trace_id == trace_id)
        if event_type is not None:
            stmt = stmt.where(OutboxEventModel.event_type == event_type)
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(OutboxEventModel.updated_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(OutboxEventModel.updated_at <= until_dt)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(
            items=tuple(row.to_domain() for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )


__all__ = ["OutboxEventRepository"]
