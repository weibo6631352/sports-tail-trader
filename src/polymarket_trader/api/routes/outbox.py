from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.deps import build_time_range, get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/outbox", tags=["outbox"])


@router.get("/queue-depth")
async def get_queue_depth(
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """事件队列积压深度（纯内存，零 DB，零 P0 影响）。

    返回 trading / maintenance / persistence 三条 lane 的实时队列深度与容量。
    持久化 lane 积压过高说明 DB 写入出现瓶颈。
    """

    return service.outbox_queue_depth()


@router.get("/pending")
async def list_outbox_pending(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    trace_id: str | None = Query(default=None),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.list_outbox_pending(
        limit=limit,
        offset=offset,
        trace_id=trace_id,
    )


@router.get("/failures")
async def list_outbox_failures(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    trace_id: str | None = Query(default=None),
    event_type: str | None = Query(default=None, min_length=1, max_length=128),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    min_retry_count: int = Query(default=1, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """Outbox 失败/重试事件。

    DB 是唯一真相来源——筛选 ``retry_count >= min_retry_count`` 或带
    ``last_error`` 的事件，按 ``updated_at`` 倒序，排查持久化链路问题刚需。
    """

    return await service.list_outbox_failures(
        limit=limit,
        offset=offset,
        trace_id=trace_id,
        event_type=event_type,
        time_range=build_time_range(since=since, until=until),
        min_retry_count=min_retry_count,
    )
