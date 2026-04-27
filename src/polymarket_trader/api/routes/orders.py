from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/orders", tags=["orders"])


class ReplaceOrderRequest(BaseModel):
    order_id: str = Field(min_length=1)
    market_slug: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    new_price: Decimal = Field(gt=Decimal("0"), lt=Decimal("1"))
    size_shares: Decimal | None = Field(default=None, gt=Decimal("0"))
    operator: str = "manual"
    reason: str = "admin_replace_order"
    trace_id: str | None = None


@router.get("")
async def list_orders(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    open_only: bool = Query(default=True),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    trace_id: str | None = Query(default=None),
    order_id: str | None = Query(default=None),
    trade_id: str | None = Query(default=None),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.list_orders(
        limit=limit,
        offset=offset,
        open_only=open_only,
        condition_id=condition_id,
        token_id=token_id,
        trace_id=trace_id,
        order_id=order_id,
        trade_id=trade_id,
    )


@router.post("/replace")
async def replace_order(
    request: ReplaceOrderRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.replace_order(
        order_id=request.order_id,
        market_slug=request.market_slug,
        condition_id=request.condition_id,
        token_id=request.token_id,
        new_price=request.new_price,
        size_shares=request.size_shares,
        operator=request.operator,
        reason=request.reason,
        trace_id=request.trace_id,
    )
