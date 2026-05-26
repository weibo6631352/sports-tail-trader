from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.aggregators import AnalyticsAggregator, ReconcileDecisionsAggregator
from polymarket_trader.api.deps import build_time_range, get_operator_service, get_runtime
from polymarket_trader.api.middleware.rate_limit import rate_limit
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


class ParameterSweepRequest(BaseModel):
    """参数扫描请求。

    ``candidates`` 是 ``{参数键: 候选值列表}``；笛卡尔积上限 1000 由 service
    侧守门。``per_decision_usdc`` 控制单笔模拟仓位规模，默认 10 USDC。
    ``since`` / ``until`` 限定回放窗口（ms）。
    """

    candidates: dict[str, list[Any]] = Field(default_factory=dict)
    # 用 Decimal 直接接 JSON 数字/字符串：float 中间桥接会在反序列化时引入精度
    # 漂移（CLAUDE.md「金额/价格用 Decimal，不用浮点」）。ge=0.01 (1 美分)
    # 防止 1e-300 / 1e-9 这种亚精度值传到 service。
    per_decision_usdc: Decimal = Field(
        default=Decimal("10.0"),
        ge=Decimal("0.01"),
        le=Decimal("100000.0"),
    )
    since: int | None = Field(default=None, ge=0)
    until: int | None = Field(default=None, ge=0)
    decision_limit: int = Field(default=2000, ge=1, le=20_000)
    settlement_limit: int = Field(default=2000, ge=1, le=20_000)


# Polymarket condition_id 是 32-byte hash, 0x 前缀 + 64 hex 字符。
# token_id 是 78-位十进制大整数（uint256）。
# 这两个正则是 Polymarket 协议天然给定的格式，不是策略层口味——任何不符合
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


@router.get("/parameter-sweep/params")
async def parameter_sweep_params() -> list[dict[str, object]]:
    """返回所有可调参数的元数据（类型、范围、标签、示例），供前端渲染 UI。

    单一来源：前端不再维护本地副本，新增/删除参数只改 parameter_sweep.py。
    """
    from polymarket_trader.domain.analytics.parameter_sweep import supported_parameter_specs
    return supported_parameter_specs()


@router.post("/parameter-sweep")
async def parameter_sweep(
    request: ParameterSweepRequest,
    runtime: Any = Depends(get_runtime),
    _rate: None = Depends(rate_limit(endpoint="parameter_sweep", qps=0.5, burst=2)),
) -> dict[str, object]:
    """对历史决策回放给定参数候选笛卡尔积，输出每组 hypothetical PnL 排序。"""

    aggregator = AnalyticsAggregator(session_factory=runtime.db_session_factory)
    try:
        return await aggregator.run_parameter_sweep(
            candidates=request.candidates,
            per_decision_usdc=request.per_decision_usdc,
            time_range=build_time_range(since=request.since, until=request.until),
            decision_limit=request.decision_limit,
            settlement_limit=request.settlement_limit,
        )
    except ValueError as exc:
        # ValueError 的文本可能含内部 schema 细节，不能透传给客户端。固定错误
        # 码 + trace_id 让运维侧在日志里反查具体原因，§10 可审计性保留。
        trace_id = uuid4().hex
        logger.warning(
            "parameter_sweep validation failed",
            extra={"trace_id": trace_id, "error": str(exc)},
        )
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "parameter_sweep_validation_failed",
                "trace_id": trace_id,
            },
        ) from exc


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
