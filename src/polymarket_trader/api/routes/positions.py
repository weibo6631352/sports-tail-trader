from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("")
async def list_positions(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.list_positions(
        limit=limit,
        offset=offset,
        condition_id=condition_id,
        token_id=token_id,
    )
