from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.aggregators import ReconcileDecisionsAggregator
from polymarket_trader.api.deps import build_time_range, get_operator_service, get_runtime
from polymarket_trader.app.operator_service import OperatorService
from polymarket_trader.app.virtual_paper_trading import run_virtual_paper_trade

router = APIRouter(prefix="/operations", tags=["operations"])
logger = logging.getLogger(__name__)


class ReconcileRequest(BaseModel):
    trace_id: str | None = None
    # 上限 200 防 DoS：reconciler 在 batch 扫描里读热状态，违反 CLAUDE.md §7
    # 「reconciler 不在批量扫描里长时间持有交易状态写锁」就会反向阻塞主链路。
    condition_ids: list[str] = Field(default_factory=list, max_length=200)
    reason: str = Field(default="", max_length=256)
    authorized_by: str = Field(default="operator", min_length=1, max_length=64)


# Polymarket condition_id 是 32-byte hash, 0x 前缀 + 64 hex 字符。
# token_id 是 78-位十进制大整数（uint256）。
# 这两个正则是 Polymarket 协议天然给定的格式，不是workflow 层口味——任何不符合
# 这个格式的字符串都不可能是真实市场 ID，应当 422 拦下。
_CONDITION_ID_PATTERN = r"^0x[0-9a-fA-F]{64}$"
_TOKEN_ID_PATTERN = r"^[0-9]+$"


class VirtualPaperTradeRequest(BaseModel):
    condition_id: str | None = Field(default=None, pattern=_CONDITION_ID_PATTERN)
    token_id: str | None = Field(default=None, pattern=_TOKEN_ID_PATTERN, min_length=1, max_length=80)
    market_slug: str | None = Field(default=None, min_length=1, max_length=200)


class PauseTradingRequest(BaseModel):
    # reason 进审计日志，限长防 DoS / 存储溢出。
    reason: str = Field(default="manual_pause", min_length=1, max_length=200)
    operator: str = Field(default="manual", min_length=1, max_length=64)
    authorized_by: str = Field(default="operator", min_length=1, max_length=64)
    # 前端 confirmAction 生成；后端 audit_events 用它串"操作意图 + 审计事件"。
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


class ResumeTradingRequest(BaseModel):
    operator: str = Field(default="manual", min_length=1, max_length=64)
    reason: str = Field(default="", max_length=256)
    authorized_by: str = Field(default="operator", min_length=1, max_length=64)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


@router.post("/reconcile")
async def reconcile(
    request: ReconcileRequest,
    service: OperatorService = Depends(get_operator_service),
) -> dict[str, object]:
    return await service.reconcile(
        trace_id=request.trace_id,
        condition_ids=tuple(request.condition_ids),
        reason=request.reason,
        authorized_by=request.authorized_by,
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
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """Reconcile diff 结构化视图。"""

    aggregator = ReconcileDecisionsAggregator(session_factory=runtime.db_session_factory)
    return await aggregator.list_reconcile_diffs(
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
    service: OperatorService = Depends(get_operator_service),
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
    service: OperatorService = Depends(get_operator_service),
) -> dict[str, object]:
    return await service.pause_trading(
        reason=request.reason,
        operator=request.operator,
        authorized_by=request.authorized_by,
        trace_id=request.trace_id,
    )


@router.post("/resume-trading")
async def resume_trading(
    request: ResumeTradingRequest,
    service: OperatorService = Depends(get_operator_service),
) -> dict[str, object]:
    return await service.resume_trading(
        operator=request.operator,
        reason=request.reason,
        authorized_by=request.authorized_by,
        trace_id=request.trace_id,
    )
