from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.aggregators import OutboxAggregator, RuntimeAggregator
from polymarket_trader.api.deps import build_time_range, get_runtime

router = APIRouter(prefix="/outbox", tags=["outbox"])


@router.get("/queue-depth")
async def get_queue_depth(runtime: Any = Depends(get_runtime)) -> dict[str, object]:
    """事件队列积压深度（纯内存，零 DB，零 P0 影响）。"""

    return RuntimeAggregator(runtime=runtime).outbox_queue_depth()


@router.get("/pending")
async def list_outbox_pending(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    trace_id: str | None = Query(default=None),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """Pending events（走 OutboxAggregator）—— 走 runtime.outbox（内存），无 DB。"""
    aggregator = OutboxAggregator(
        session_factory=runtime.db_session_factory,
        runtime_outbox=runtime.outbox,
    )
    return await aggregator.list_pending(limit=limit, offset=offset, trace_id=trace_id)


@router.get("/failures")
async def list_outbox_failures(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    trace_id: str | None = Query(default=None),
    event_type: str | None = Query(default=None, min_length=1, max_length=128),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    min_retry_count: int = Query(default=1, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """Outbox 失败/重试事件（走 OutboxAggregator）—— DB 是唯一真相，进程崩溃后
    pending 内存丢，必须从 DB 拉历史失败事件。"""

    aggregator = OutboxAggregator(
        session_factory=runtime.db_session_factory,
        runtime_outbox=runtime.outbox,
    )
    return await aggregator.list_failures(
        limit=limit,
        offset=offset,
        trace_id=trace_id,
        event_type=event_type,
        time_range=build_time_range(since=since, until=until),
        min_retry_count=min_retry_count,
    )
