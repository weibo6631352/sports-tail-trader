from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncIterator, Iterable, Sequence

from sqlalchemy import select, update

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
                "payload_hash",
                "last_seen_at",
                "updated_at",
            ),
        )

    async def bump_occurrences(
        self,
        bumps: Sequence[tuple[str, datetime]],
    ) -> int:
        """原架构方案 §13.4：dedupe 窗口内重复事件 → UPDATE 原始行。

        ``bumps`` 是 ``(event_id, last_seen_at)`` 列表；每条让 occurrence_count + 1
        且更新 last_seen_at。同一 event_id 出现多次时分多次 UPDATE（不在 SQL 里聚合）——
        每个 OutboxEvent 都代表一次实际触发。
        """

        if not bumps:
            return 0
        affected = 0
        for event_id, last_seen_at in bumps:
            stmt = (
                update(AuditEventModel)
                .where(AuditEventModel.event_id == event_id)
                .values(
                    occurrence_count=AuditEventModel.occurrence_count + 1,
                    last_seen_at=last_seen_at,
                )
            )
            result = await self._session.execute(stmt)
            affected += int(result.rowcount or 0)
        return affected

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

    async def list_by_condition_and_titles(
        self,
        *,
        condition_id: str,
        event_titles: Sequence[str],
        time_range: TimeRange | None = None,
        per_channel_limit: int = 50,
    ) -> tuple[AuditEvent, ...]:
        """单 SQL 按 condition + event_title IN (...) 拉取审计事件。

        利用 ``ix_audit_events_condition_id`` + ``ix_audit_events_event_title``
        命中索引；总 LIMIT = per_channel_limit * N channels（保证每个 channel
        在最坏情况也能拿满，调用方按 channel 分组截断）。

        返回按 ``created_at`` 倒序的 AuditEvent 元组，按 channel 的分组由
        调用方完成（这里不绑定输出形态）。
        """

        if not event_titles:
            return ()
        per_channel_limit = max(1, min(per_channel_limit, 500))
        stmt = (
            select(AuditEventModel)
            .where(AuditEventModel.condition_id == condition_id)
            .where(AuditEventModel.event_title.in_(tuple(event_titles)))
            .order_by(AuditEventModel.created_at.desc(), AuditEventModel.id.desc())
            .limit(per_channel_limit * len(event_titles))
        )
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(AuditEventModel.created_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(AuditEventModel.created_at <= until_dt)
        rows = (await self._session.execute(stmt)).scalars().all()
        return tuple(row.to_domain() for row in rows)

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
