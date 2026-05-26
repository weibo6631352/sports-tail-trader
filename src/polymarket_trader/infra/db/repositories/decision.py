from __future__ import annotations

from typing import Iterable

from sqlalchemy import select

from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db.models import DecisionRecordModel
from polymarket_trader.infra.db.repositories._base import (
    BaseRepository,
    RepositoryPage,
    _limit_offset,
    _row_dict,
)


class DecisionRecordRepository(BaseRepository):
    """决策录制仓储。

    Append-only：每条决策一行；查询走 ``created_at`` 倒序，
    `dump` 端点按 trace_id / condition_id / accepted / 时间窗过滤。
    """

    async def save_decision_record(self, record: DecisionRecord) -> int:
        return await self.save_decision_records([record])

    async def get_by_record_id(self, record_id: str) -> DecisionRecord | None:
        stmt = select(DecisionRecordModel).where(DecisionRecordModel.record_id == record_id).limit(1)
        row = await self._session.scalar(stmt)
        return None if row is None else row.to_domain()

    async def save_decision_records(self, records: Iterable[DecisionRecord]) -> int:
        records = tuple(records)
        if not records:
            return 0
        rows = [_row_dict(DecisionRecordModel.from_domain(record)) for record in records]
        # ON CONFLICT DO NOTHING 防御性兜底：record_id 唯一，正常情况下不会冲突；
        # 但 outbox retry 可能重复投递同一事件，幂等忽略保证不破坏审计行。
        return await self._bulk_upsert(
            DecisionRecordModel,
            rows,
            conflict_columns=("record_id",),
            update_columns=(),
        )

    async def list_decisions_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        condition_id: str | None = None,
        accepted: bool | None = None,
        time_range: TimeRange | None = None,
    ) -> RepositoryPage[DecisionRecord]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(DecisionRecordModel).order_by(
            DecisionRecordModel.created_at.desc(), DecisionRecordModel.id.desc()
        )
        if trace_id is not None:
            stmt = stmt.where(DecisionRecordModel.trace_id == trace_id)
        if condition_id is not None:
            stmt = stmt.where(DecisionRecordModel.condition_id == condition_id)
        if accepted is not None:
            stmt = stmt.where(DecisionRecordModel.accepted == accepted)
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(DecisionRecordModel.created_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(DecisionRecordModel.created_at <= until_dt)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(
            items=tuple(row.to_domain() for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )


__all__ = ["DecisionRecordRepository"]
