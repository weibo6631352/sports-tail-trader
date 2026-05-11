from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.deps import build_time_range, get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(tags=["runtime"])

_DECISIONS_DUMP_DEFAULT_LIMIT = 1000
_DECISIONS_DUMP_MAX_LIMIT = 10000


@router.get("/runtime")
async def runtime(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return await service.runtime_snapshot()


@router.get("/workers")
async def workers(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return service.workers_snapshot()


@router.get("/metrics")
async def metrics(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return service.metrics_snapshot()


@router.get("/admin/decisions/dump")
async def dump_decision_records(
    limit: int = Query(default=_DECISIONS_DUMP_DEFAULT_LIMIT, ge=1, le=_DECISIONS_DUMP_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    trace_id: str | None = Query(default=None),
    condition_id: str | None = Query(default=None),
    accepted: bool | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """从 ``decision_records`` 表分页查询历史决策。

    DB 是该接口唯一真相来源；进程内存中不再维护 ring buffer，无 DB 时直接返回空集。
    """

    return await service.list_decisions(
        limit=limit,
        offset=offset,
        trace_id=trace_id,
        condition_id=condition_id,
        accepted=accepted,
        time_range=build_time_range(since=since, until=until),
        strategy_id=strategy_id,
    )
