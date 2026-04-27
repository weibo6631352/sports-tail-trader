from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/operations", tags=["operations"])


class ReconcileRequest(BaseModel):
    trace_id: str | None = None
    condition_ids: list[str] = Field(default_factory=list)


@router.post("/reconcile")
async def reconcile(
    request: ReconcileRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.reconcile(
        trace_id=request.trace_id,
        condition_ids=tuple(request.condition_ids),
    )
