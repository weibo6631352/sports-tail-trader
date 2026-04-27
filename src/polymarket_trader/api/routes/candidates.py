from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/candidates", tags=["candidates"])


class SportsLiveStateRequest(BaseModel):
    """人工或外部采集器写入的体育直播状态。"""

    sports_tail_game: dict[str, Any] = Field(default_factory=dict)
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
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.list_sports_tail_candidates(
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


@router.get("/live-states")
async def list_live_states(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.list_sports_live_states(limit=limit, offset=offset)


@router.post("/live-states")
async def upsert_live_state(
    request: SportsLiveStateRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    if not any((request.condition_id, request.market_slug, request.event_slug)):
        raise HTTPException(
            status_code=422,
            detail="condition_id, market_slug, or event_slug is required",
        )
    return await service.upsert_sports_live_state(
        sports_tail_game=request.sports_tail_game,
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
    return await service.confirm_sports_tail_candidate(
        condition_id=request.condition_id,
        token_id=request.token_id,
        market_slug=request.market_slug,
        operator=request.operator,
        note=request.note,
        trace_id=request.trace_id,
    )
