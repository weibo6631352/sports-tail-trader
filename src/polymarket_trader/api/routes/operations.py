from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.app.virtual_paper_trading import run_virtual_paper_trade

router = APIRouter(prefix="/operations", tags=["operations"])


class ReconcileRequest(BaseModel):
    trace_id: str | None = None
    condition_ids: list[str] = Field(default_factory=list)


class VirtualPaperTradeRequest(BaseModel):
    condition_id: str | None = None
    token_id: str | None = None
    market_slug: str | None = None


@router.post("/reconcile")
async def reconcile(
    request: ReconcileRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.reconcile(
        trace_id=request.trace_id,
        condition_ids=tuple(request.condition_ids),
    )


@router.post("/virtual-paper-trade")
async def virtual_paper_trade(
    request: VirtualPaperTradeRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    runtime = service.runtime
    if runtime is None:
        return {
            "status": "failed",
            "reason": "runtime_unavailable",
            "data_source": "real_runtime",
            "execution": "not_submitted",
        }
    return await run_virtual_paper_trade(
        runtime,
        condition_id=request.condition_id,
        token_id=request.token_id,
        market_slug=request.market_slug,
    )
