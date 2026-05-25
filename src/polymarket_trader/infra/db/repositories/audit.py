from __future__ import annotations

from typing import Any, AsyncIterator, Iterable, Sequence

from sqlalchemy import select

from polymarket_trader.domain.events import AuditEvent
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db.models import AuditEventModel
from polymarket_trader.infra.db.repositories._base import (
    BaseRepository,
    RepositoryPage,
    _limit_offset,
    _row_dict,
)


class AuditEventRepository(BaseRepository):
    """审计事件仓储。"""

    async def save_audit_event(self, event: AuditEvent, *, raw_payload: dict[str, Any] | None = None) -> AuditEvent:
        await self.save_audit_events([event], raw_payloads=[raw_payload])
        return event

    async def save_audit_events(
        self,
        events: Iterable[AuditEvent],
        *,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        events = tuple(events)
        payloads = raw_payloads or (None,) * len(events)
        rows = [
            _row_dict(AuditEventModel.from_domain(event, raw_payload=payload))
            for event, payload in zip(events, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            AuditEventModel,
            rows,
            conflict_columns=("event_id",),
            update_columns=(
                "trace_id",
                "event_title",
                "market_slug",
                "event_slug",
                "condition_id",
                "token_id",
                "outcome",
                "side",
                "order_type",
                "price",
                "size",
                "notional_usdc",
                "order_id",
                "trade_id",
                "tx_hash",
                "status",
                "reason",
                "raw_response",
                "payload",
                "updated_at",
            ),
        )

    async def list_audit_events_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        event_title: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        time_range: TimeRange | None = None,
        with_total: bool = True,
    ) -> RepositoryPage[AuditEvent]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(AuditEventModel).order_by(AuditEventModel.created_at.desc(), AuditEventModel.id.desc())
        if trace_id is not None:
            stmt = stmt.where(AuditEventModel.trace_id == trace_id)
        if event_title is not None:
            stmt = stmt.where(AuditEventModel.event_title == event_title)
        if condition_id is not None:
            stmt = stmt.where(AuditEventModel.condition_id == condition_id)
        if token_id is not None:
            stmt = stmt.where(AuditEventModel.token_id == token_id)
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(AuditEventModel.created_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(AuditEventModel.created_at <= until_dt)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset, with_total=with_total)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)

    async def stream_audit_events_in_range(
        self,
        *,
        time_range: TimeRange | None,
        limit: int,
    ) -> AsyncIterator[AuditEventModel]:
        """按 created_at 时间窗流式拉取审计事件 ORM 行，供 export 端点消费。"""

        stmt = select(AuditEventModel).order_by(
            AuditEventModel.created_at.asc(), AuditEventModel.id.asc()
        )
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(AuditEventModel.created_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(AuditEventModel.created_at <= until_dt)
        stmt = stmt.limit(limit)
        result = await self._session.stream_scalars(stmt)
        async for row in result:
            yield row


__all__ = ["AuditEventRepository"]
