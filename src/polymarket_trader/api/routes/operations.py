from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import build_time_range, get_admin_service
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


class PauseTradingRequest(BaseModel):
    reason: str = Field(default="manual_pause", min_length=1)
    operator: str = "manual"


class ResumeTradingRequest(BaseModel):
    operator: str = "manual"


@router.post("/reconcile")
async def reconcile(
    request: ReconcileRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.reconcile(
        trace_id=request.trace_id,
        condition_ids=tuple(request.condition_ids),
    )


@router.get("/reconcile/diffs")
async def list_reconcile_diffs(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    trace_id: str | None = Query(default=None),
    condition_id: str | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    include_started: bool = Query(default=False),
    include_applied: bool = Query(default=True),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """Reconcile diff 结构化视图。

    从 outbox_events 中筛 ``reconcile_diff_detected`` / ``reconcile_applied`` /
    可选 ``reconcile_started``，payload 含 action_type / target_size_shares /
    target_notional_usdc / pause_reason / metadata，回答"对账在修什么、修了
    几次、根因分布"。
    """

    return await service.list_reconcile_diffs(
        limit=limit,
        offset=offset,
        trace_id=trace_id,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
        include_started=include_started,
        include_applied=include_applied,
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


@router.post("/pause-trading")
async def pause_trading(
    request: PauseTradingRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.pause_trading(reason=request.reason, operator=request.operator)


@router.post("/resume-trading")
async def resume_trading(
    request: ResumeTradingRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.resume_trading(operator=request.operator)
