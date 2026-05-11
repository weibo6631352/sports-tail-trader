from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/positions", tags=["positions"])


class ForceExitRequest(BaseModel):
    condition_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    price: Decimal | None = Field(default=None, gt=Decimal("0"), lt=Decimal("1"))
    operator: str = "manual"
    reason: str = "admin_force_exit"
    trace_id: str | None = None


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


@router.post("/force-exit")
async def force_exit(
    request: ForceExitRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.force_exit_position(
        condition_id=request.condition_id,
        token_id=request.token_id,
        price=request.price,
        operator=request.operator,
        reason=request.reason,
        trace_id=request.trace_id,
    )
