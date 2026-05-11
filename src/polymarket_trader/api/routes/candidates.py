from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/candidates", tags=["candidates"])


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
    limit: int = Query(default=100, ge=1, le=500),
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
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
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
        strategy_id=strategy_id,
    )


@router.get("/live-states")
async def list_live_states(
    limit: int = Query(default=100, ge=1, le=500),
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
    request: LiveStateRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    if not any((request.condition_id, request.market_slug, request.event_slug)):
        raise HTTPException(
            status_code=422,
            detail="condition_id, market_slug, or event_slug is required",
        )
    return await service.upsert_live_state(
        payload=request.payload,
        signal_allowed=request.signal_allowed,
        signal_reason=request.signal_reason,
        condition_id=request.condition_id,
        market_slug=request.market_slug,
        event_slug=request.event_slug,
        source=request.source,
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
