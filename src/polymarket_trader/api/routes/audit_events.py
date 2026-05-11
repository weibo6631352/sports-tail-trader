from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.deps import build_time_range, get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/audit-events", tags=["audit-events"])


@router.get("")
async def list_audit_events(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    trace_id: str | None = Query(default=None),
    event_title: str | None = Query(default=None),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.list_audit_events(
        limit=limit,
        offset=offset,
        trace_id=trace_id,
        event_title=event_title,
        condition_id=condition_id,
        token_id=token_id,
        time_range=build_time_range(since=since, until=until),
    )
