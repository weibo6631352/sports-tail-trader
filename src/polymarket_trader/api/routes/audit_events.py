from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.aggregators import TimelineAggregator
from polymarket_trader.api.deps import build_time_range, get_admin_service, get_runtime
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/audit-events", tags=["audit-events"])


@router.get("")
async def list_audit_events(
    # 上限 500 → 5000: reconcile 每 30s 产 2 条,500 条只覆盖 ~4h 复盘,
    # 跨多事件类型聚合时被 reconcile 占满。5000 给操盘复盘足够空间。
    limit: int = Query(default=100, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    trace_id: str | None = Query(default=None),
    event_title: str | None = Query(default=None),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    # 默认 False:大表 count subquery 每次 ~870ms,monitor 高频拉取不需要 total.
    # 前端如需总数显式传 include_total=true.
    include_total: bool = Query(default=False),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """走 TimelineAggregator（基于 DB session_factory）—— admin_query/timeline.py 已被替代。"""
    aggregator = TimelineAggregator(session_factory=runtime.db_session_factory)
    return await aggregator.list_audit_events(
        limit=limit,
        offset=offset,
        trace_id=trace_id,
        event_title=event_title,
        condition_id=condition_id,
        token_id=token_id,
        time_range=build_time_range(since=since, until=until),
        with_total=include_total,
    )


@router.get("/operators")
async def aggregate_operator_interventions(
    operator: str | None = Query(default=None, min_length=1, max_length=64),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    sample_limit: int = Query(default=2000, ge=1, le=10_000),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """人工干预审计聚合——按 ``operator`` 分组事件计数 + 类型分布。

    ``operator=None`` 时返回所有 operator 的总分布；指定后返回该 operator
    的事件类型分布 + 最近 200 条详情。回答"谁动了什么"。
    """

    return await service.aggregate_operator_interventions(
        operator=operator,
        time_range=build_time_range(since=since, until=until),
        sample_limit=sample_limit,
    )
