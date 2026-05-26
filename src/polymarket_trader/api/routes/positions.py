from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.aggregators import PositionAggregator
from polymarket_trader.api.deps import get_operator_service, get_runtime
from polymarket_trader.app.operator_service import OperatorService

router = APIRouter(prefix="/positions", tags=["positions"])


class ForceExitRequest(BaseModel):
    condition_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    price: Decimal | None = Field(default=None, gt=Decimal("0"), lt=Decimal("1"))
    operator: str = "manual"
    reason: str = "operator_force_exit"
    trace_id: str | None = None


@router.get("")
async def list_positions(
    level: Literal["summary", "detail"] = Query("summary"),
    only_with_shares: bool = Query(True),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """持仓列表（基于 DataGraph，按 position_usdc 降序，§17 不丢机会原则）。

    `level=summary` 仅核心字段；`detail` 含完整 PnL / 挂单 / 确认状态。
    `only_with_shares=False` 包含 settled_zero_value 等已结算仓位。
    """

    aggregator = PositionAggregator(data_graph=runtime.data_graph)
    items = aggregator.list_positions(level=level, only_with_shares=only_with_shares)
    return {"positions": list(items), "count": len(items)}


@router.post("/force-exit")
async def force_exit(
    request: ForceExitRequest,
    service: OperatorService = Depends(get_operator_service),
) -> dict[str, object]:
    return await service.force_exit_position(
        condition_id=request.condition_id,
        token_id=request.token_id,
        price=request.price,
        operator=request.operator,
        reason=request.reason,
        trace_id=request.trace_id,
    )
