from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import build_time_range, get_admin_service
from polymarket_trader.api.rate_limit import rate_limit
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/orders", tags=["orders"])

# Polymarket condition_id 是 32-byte hash, 0x 前缀 + 64 hex 字符。
# token_id 是 78-位十进制大整数（uint256）。这两个格式由协议给定，
# 任何不符合的字符串都不可能是真实市场 ID，应当 422 拦下而不是放进 admin service。
_CONDITION_ID_PATTERN = r"^0x[0-9a-fA-F]{64}$"
_TOKEN_ID_PATTERN = r"^[0-9]+$"


class ReplaceOrderRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=128)
    market_slug: str | None = Field(default=None, min_length=1, max_length=200)
    condition_id: str | None = Field(default=None, pattern=_CONDITION_ID_PATTERN)
    token_id: str | None = Field(default=None, pattern=_TOKEN_ID_PATTERN, min_length=1, max_length=80)
    new_price: Decimal = Field(gt=Decimal("0"), lt=Decimal("1"))
    size_shares: Decimal | None = Field(default=None, gt=Decimal("0"))
    operator: str = Field(default="manual", min_length=1, max_length=64)
    reason: str = Field(default="admin_replace_order", min_length=1, max_length=200)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


class CancelOrderRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=128)
    market_slug: str | None = Field(default=None, min_length=1, max_length=200)
    condition_id: str | None = Field(default=None, pattern=_CONDITION_ID_PATTERN)
    token_id: str | None = Field(default=None, pattern=_TOKEN_ID_PATTERN, min_length=1, max_length=80)
    operator: str = Field(default="manual", min_length=1, max_length=64)
    reason: str = Field(default="admin_cancel_order", min_length=1, max_length=200)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


@router.get("")
async def list_orders(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    open_only: bool = Query(default=True),
    condition_id: str | None = Query(default=None, pattern=_CONDITION_ID_PATTERN),
    token_id: str | None = Query(default=None, pattern=_TOKEN_ID_PATTERN, min_length=1, max_length=80),
    trace_id: str | None = Query(default=None, min_length=1, max_length=128),
    order_id: str | None = Query(default=None, min_length=1, max_length=128),
    trade_id: str | None = Query(default=None, min_length=1, max_length=128),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
    service: AdminService = Depends(get_admin_service),
    _rate: None = Depends(rate_limit(endpoint="list_orders", qps=2.0, burst=5)),
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
        time_range=build_time_range(since=since, until=until),
        strategy_id=strategy_id,
    )


@router.post("/replace")
async def replace_order(
    request: ReplaceOrderRequest,
    service: AdminService = Depends(get_admin_service),
    _rate: None = Depends(rate_limit(endpoint="replace_order", qps=0.5, burst=2)),
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


@router.post("/cancel")
async def cancel_order(
    request: CancelOrderRequest,
    service: AdminService = Depends(get_admin_service),
    _rate: None = Depends(rate_limit(endpoint="cancel_order", qps=0.5, burst=2)),
) -> dict[str, object]:
    return await service.cancel_order(
        order_id=request.order_id,
        market_slug=request.market_slug,
        condition_id=request.condition_id,
        token_id=request.token_id,
        operator=request.operator,
        reason=request.reason,
        trace_id=request.trace_id,
    )
