from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

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


@router.get("/metrics/latency-percentiles")
async def metrics_latency_percentiles(
    window_ms: int | None = Query(default=None, ge=0),
    sample_limit: int = Query(default=500, ge=1, le=5000),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """订单执行 latency 分位数（queue→sign / sign→submit / submit→ack / queue→ack）。

    从 ``outbox_events.payload->timestamps`` 抽样最近 ``sample_limit`` 条 order
    lifecycle 事件，计算每个 stage 的 p50/p90/p95/p99 毫秒数；现网延迟劣化
    （签名变慢 / WS 卡顿）操盘观测刚需。
    """

    return await service.latency_percentiles_snapshot(
        window_ms=window_ms,
        sample_limit=sample_limit,
    )


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


@router.get("/decisions/{record_id}")
async def get_decision_record(
    record_id: str,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """按 ``record_id`` 取单条策略决策详情。

    返回完整 ``decision_input`` / ``decision_output`` JSONB——含 ``fair_value``、
    ``entry_price_cap``、``kelly_fraction``、拒绝原因枚举等策略中间量，便于从
    trade timeline 点开后做根因追查。
    """

    payload = await service.get_decision_record(record_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="decision_record_not_found")
    return payload
