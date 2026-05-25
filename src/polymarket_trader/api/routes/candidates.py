from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/candidates", tags=["candidates"])

# 256KB 对 live_state 这种"赛况快照 + 比分 + 指标"已经富余；超过这个量级
# 大概率是误填或攻击 payload。outbox / persistence 是异步副作用路径，超大
# payload 会拖慢落库，间接影响审计可见性。
_LIVE_STATE_MAX_BYTES = 256 * 1024


class LiveStateRequest(BaseModel):
    """人工或外部采集器写入的策略可见 live_state；framework 不解析 payload 字段语义。"""

    payload: dict[str, Any] = Field(default_factory=dict)
    signal_allowed: bool | None = None
    signal_reason: str = ""
    condition_id: str | None = None
    market_slug: str | None = None
    event_slug: str | None = None
    source: str = "manual"


class ConfirmCandidateRequest(BaseModel):
    """人工确认一个候选入场机会。"""

    token_id: str
    condition_id: str | None = None
    market_slug: str | None = None
    operator: str = "manual"
    note: str | None = None
    trace_id: str | None = None


@router.get("")
async def list_candidates(
    # 上限放大：候选可能上千，过小的 limit 会把正在直播比赛的候选截掉。
    limit: int = Query(default=2000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    market_slug: str | None = Query(default=None),
    market_type: str | None = Query(default=None),
    game_status: str | None = Query(default=None),
    action: str | None = Query(default=None),
    execution_permission: str | None = Query(default=None),
    accepted: bool | None = Query(default=None),
    confirmable: bool | None = Query(default=None),
    league: str | None = Query(default=None),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.list_strategy_candidates(
        limit=limit,
        offset=offset,
        condition_id=condition_id,
        token_id=token_id,
        market_slug=market_slug,
        market_type=market_type,
        game_status=game_status,
        action=action,
        execution_permission=execution_permission,
        accepted=accepted,
        confirmable=confirmable,
        league=league,
    )


@router.get("/data-freshness")
async def get_data_freshness(
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """每市场 live_state 数据源新鲜度（纯内存，零 DB，零 P0 影响）。

    从 market_metadata_store 读取每条记录的最后更新时间，计算 staleness_ms。
    staleness 过高表明该市场的 live_state 数据源已断流。
    """

    return service.data_freshness()


@router.get("/live-states")
async def list_live_states(
    # 默认上限放大到 2000：直播状态条数可能上千，过小的 limit 会把正在直播的
    # 赛事截掉。服务层已把 phase=live 排到最前，limit 再大也不漏直播。
    limit: int = Query(default=2000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.list_sports_live_states(limit=limit, offset=offset)


@router.get("/live-source-gaps")
async def list_live_source_gaps(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    prefix: str | None = Query(default=None),
    include_future_schedule: bool = Query(default=False),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.list_sports_live_source_gaps(
        limit=limit,
        offset=offset,
        prefix=prefix,
        include_future_schedule=include_future_schedule,
    )


@router.post("/live-states")
async def upsert_live_state(
    body: LiveStateRequest,
    http_request: Request,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    # Content-Length 提前拦截大 payload，避免 service / DB / outbox 被拖慢。
    # FastAPI / uvicorn 没有内建 body size limit，必须在路由层做。
    content_length = http_request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _LIVE_STATE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"live_state payload too large: {content_length} bytes > {_LIVE_STATE_MAX_BYTES}",
        )
    if not any((body.condition_id, body.market_slug, body.event_slug)):
        raise HTTPException(
            status_code=422,
            detail="condition_id, market_slug, or event_slug is required",
        )
    return await service.upsert_live_state(
        payload=body.payload,
        signal_allowed=body.signal_allowed,
        signal_reason=body.signal_reason,
        condition_id=body.condition_id,
        market_slug=body.market_slug,
        event_slug=body.event_slug,
        source=body.source,
    )


@router.post("/confirm")
async def confirm_candidate(
    request: ConfirmCandidateRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.confirm_candidate(
        condition_id=request.condition_id,
        token_id=request.token_id,
        market_slug=request.market_slug,
        operator=request.operator,
        note=request.note,
        trace_id=request.trace_id,
    )
