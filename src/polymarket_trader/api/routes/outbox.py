from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/outbox", tags=["outbox"])


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
