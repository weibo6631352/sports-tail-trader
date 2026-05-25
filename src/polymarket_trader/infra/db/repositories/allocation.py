from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy import select

from polymarket_trader.domain.allocation import Allocation
from polymarket_trader.infra.db.models import AllocationModel
from polymarket_trader.infra.db.repositories._base import (
    BaseRepository,
    RepositoryPage,
    _limit_offset,
    _row_dict,
)


class AllocationRepository(BaseRepository):
    """分配计划仓储。"""

    async def save_allocation(
        self,
        allocation: Allocation,
        *,
        trace_id: str,
        raw_payload: dict[str, Any] | None = None,
    ) -> Allocation:
        await self.save_allocations([allocation], trace_id=trace_id, raw_payloads=[raw_payload])
        return allocation

    async def save_allocations(
        self,
        allocations: Iterable[Allocation],
        *,
        trace_id: str,
        raw_payloads: Sequence[dict[str, Any] | None] | None = None,
    ) -> int:
        allocations = tuple(allocations)
        payloads = raw_payloads or (None,) * len(allocations)
        rows = [
            _row_dict(AllocationModel.from_domain(allocation, trace_id=trace_id, raw_payload=payload))
            for allocation, payload in zip(allocations, payloads, strict=False)
        ]
        return await self._bulk_upsert(
            AllocationModel,
            rows,
            conflict_columns=("allocation_key",),
            update_columns=(
                "trace_id",
                "condition_id",
                "market_slug",
                "token_id",
                "target_budget_usdc",
                "buy_budget_usdc",
                "current_exposure_usdc",
                "released_budget_usdc",
                "reason",
                "release_reason",
                "idempotency_key",
                "raw_payload",
                "updated_at",
            ),
        )

    async def list_allocations_snapshot(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        trace_id: str | None = None,
        condition_id: str | None = None,
        token_id: str | None = None,
        market_slug: str | None = None,
    ) -> RepositoryPage[Allocation]:
        limit, offset = _limit_offset(limit, offset)
        stmt = select(AllocationModel).order_by(AllocationModel.updated_at.desc(), AllocationModel.id.desc())
        if trace_id is not None:
            stmt = stmt.where(AllocationModel.trace_id == trace_id)
        if condition_id is not None:
            stmt = stmt.where(AllocationModel.condition_id == condition_id)
        if token_id is not None:
            stmt = stmt.where(AllocationModel.token_id == token_id)
        if market_slug is not None:
            stmt = stmt.where(AllocationModel.market_slug == market_slug)
        rows, total = await self._paginate(stmt, limit=limit, offset=offset)
        return RepositoryPage(items=tuple(row.to_domain() for row in rows), total=total, limit=limit, offset=offset)


__all__ = ["AllocationRepository"]
