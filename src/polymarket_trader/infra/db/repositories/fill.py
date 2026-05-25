from __future__ import annotations

from typing import Any, AsyncIterator, Iterable, Sequence

from sqlalchemy import func, select

from polymarket_trader.domain.events import Fill
from polymarket_trader.domain.time_filters import TimeRange
from polymarket_trader.infra.db.models import FillModel
from polymarket_trader.infra.db.repositories._base import (
    BaseRepository,
    RepositoryPage,
    _limit_offset,
    _row_dict,
)


class FillRepository(BaseRepository):
    """成交记录仓储。"""

    async def save_fill(self, fill: Fill, *, raw_payload: dict[str, Any] | None = None) -> Fill:
        await self.save_fills([fill], raw_payloads=[raw_payload])
        return fill

    async def save_fills(
        self,
        fills: Iterable[Fill],
        *,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        fills = tuple(fills)
        payloads = raw_payloads or (None,) * len(fills)
        rows = [
            _row_dict(FillModel.from_domain(fill, raw_payload=payload))
            for fill, payload in zip(fills, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            FillModel,
            rows,
            conflict_columns=("event_id",),
            update_columns=(
                "trace_id",
                "event_type",
                "condition_id",
                "token_id",
                "market_slug",
                "order_id",
                "trade_id",
                "side",
                "price",
                "size",
                "notional_usdc",
                "status",
                "confirmed_at",
                "raw_payload",
                "updated_at",
            ),
        )

    async def list_fills_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        order_id: str | None = None,
        trade_id: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        time_range: TimeRange | None = None,
    ) -> RepositoryPage[Fill]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(FillModel).order_by(FillModel.confirmed_at.desc(), FillModel.id.desc())
        if trace_id is not None:
            stmt = stmt.where(FillModel.trace_id == trace_id)
        if order_id is not None:
            stmt = stmt.where(FillModel.order_id == order_id)
        if trade_id is not None:
            stmt = stmt.where(FillModel.trade_id == trade_id)
        if condition_id is not None:
            stmt = stmt.where(FillModel.condition_id == condition_id)
        if token_id is not None:
            stmt = stmt.where(FillModel.token_id == token_id)
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(FillModel.created_at >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(FillModel.created_at <= until_dt)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)

    async def stream_fills_in_range(
        self,
        *,
        time_range: TimeRange | None,
        limit: int,
    ) -> AsyncIterator[FillModel]:
        """按 confirmed_at 时间窗流式拉取成交 ORM 行。

        confirmed_at 可空：对 NULL 行用 created_at 兜底，确保未确认 fill 也能被
        导出窗口看到，不至于在审计时被静默丢掉。
        """

        time_column = func.coalesce(FillModel.confirmed_at, FillModel.created_at)
        stmt = select(FillModel).order_by(time_column.asc(), FillModel.id.asc())
        if time_range is not None and not time_range.is_empty:
            since_dt, until_dt = time_range.to_datetime_range()
            if since_dt is not None:
                stmt = stmt.where(time_column >= since_dt)
            if until_dt is not None:
                stmt = stmt.where(time_column <= until_dt)
        stmt = stmt.limit(limit)
        result = await self._session.stream_scalars(stmt)
        async for row in result:
            yield row


__all__ = ["FillRepository"]
