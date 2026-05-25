from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.deps import build_time_range, get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/allocations", tags=["allocations"])


@router.get("")
async def list_allocations(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    trace_id: str | None = Query(default=None),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    market_slug: str | None = Query(default=None),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.list_allocations(
        limit=limit,
        offset=offset,
        trace_id=trace_id,
        condition_id=condition_id,
        token_id=token_id,
        market_slug=market_slug,
    )


@router.get("/decisions")
async def list_allocation_decisions(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    condition_id: str | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """AllocationPlan 决策过程历史。

    payload 含 candidates / selected_condition_ids / skipped_reasons / budget
    ——回答"为什么选这个市场、不选那个"。
    """

    return await service.list_allocation_decisions(
        limit=limit,
        offset=offset,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )
