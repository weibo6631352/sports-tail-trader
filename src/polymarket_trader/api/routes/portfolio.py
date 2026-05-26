from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from polymarket_trader.api.aggregators import (
    AnalyticsAggregator,
    PortfolioAggregator,
    RuntimeAggregator,
)
from polymarket_trader.api.deps import get_runtime
from polymarket_trader.domain.analytics.portfolio_history_service import (
    DEFAULT_INTERVAL_MS,
    DEFAULT_WINDOW_MS,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

# 单次 pnl_breakdown 上限——20000 positions × markets join 在退化数据下可能跑数十秒，
# 不能让 operator 查询长期占用 DB 连接（§7 不阻塞 P0）。超时改为 504 + 提示降 limit。
_PNL_BREAKDOWN_TIMEOUT_SECONDS = 30.0


def _runtime_error_detail(exc: RuntimeError) -> str:
    """503 detail 收紧：只暴露已知常量码，其他 RuntimeError 一律 generic + log。

    portfolio 端点底层会触碰 DB / runtime 状态；未来 lib 抛 RuntimeError 时
    message 可能含 schema 字段或栈帧，直接透传到 HTTP detail 是潜在信息泄漏。
    白名单内的常量码（service 自己 raise）安全透传，其他统一 generic。
    """

    text = str(exc).strip()
    # service / runtime 自己拼的常量码——形如 "db_session_factory_unavailable"，无 whitespace
    if text and "_" in text and " " not in text and len(text) <= 80:
        return text
    logger.warning("portfolio endpoint runtime error (detail suppressed): %s", text)
    return "portfolio_service_unavailable"


@router.get("")
async def get_portfolio(runtime: Any = Depends(get_runtime)) -> dict[str, object]:
    """组合快照——纯内存（余额 / 持仓 PnL / 暴露聚合）,零 DB。

    前端首屏高频刷新,不能拖 DB。需要近期 allocation 历史走
    ``GET /allocations?limit=N``。
    """
    aggregator = RuntimeAggregator(runtime=runtime)
    return aggregator.portfolio_snapshot()


@router.get("/exposure")
async def get_portfolio_exposure(
    level: Literal["summary", "detail"] = Query("summary"),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """每市场未平仓名义暴露（基于 DataGraph，零 DB，零 P0 影响）。

    `level=summary` 仅总览（10 字段）；`level=detail` 含 per-market outcomes /
    metadata / pause 完整数据。原架构方案 §12.3 ②字段选择。
    """

    aggregator = PortfolioAggregator(data_graph=runtime.data_graph)
    view = aggregator.exposure(level=level)
    return {"summary": view.summary, "markets": list(view.markets)}


@router.get("/equity-curve")
async def get_equity_curve(
    window_ms: int = Query(default=DEFAULT_WINDOW_MS, ge=1),
    interval_ms: int = Query(default=DEFAULT_INTERVAL_MS, ge=1),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    aggregator = AnalyticsAggregator(session_factory=runtime.db_session_factory)
    try:
        return await aggregator.portfolio_equity_curve(
            window_ms=window_ms,
            interval_ms=interval_ms,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=_runtime_error_detail(exc)) from exc


@router.get("/risk-metrics")
async def get_risk_metrics(
    window_ms: int = Query(default=DEFAULT_WINDOW_MS, ge=1),
    interval_ms: int = Query(default=DEFAULT_INTERVAL_MS, ge=1),
    annualization_factor: float | None = Query(default=None, gt=0.0, le=10_000.0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """组合级风险归因。"""

    aggregator = AnalyticsAggregator(session_factory=runtime.db_session_factory)
    try:
        return await aggregator.portfolio_risk_metrics(
            window_ms=window_ms,
            interval_ms=interval_ms,
            annualization_factor=annualization_factor,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=_runtime_error_detail(exc)) from exc


@router.get("/pnl-breakdown")
async def get_pnl_breakdown(
    group_by: str = Query(
        default="market_slug",
        description="market_slug | condition_id | category | outcome | redeemable_status",
    ),
    condition_id: str | None = Query(default=None, min_length=1),
    position_limit: int = Query(default=5000, ge=1, le=20000),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """按维度分解的 PnL 聚合。"""

    aggregator = AnalyticsAggregator(session_factory=runtime.db_session_factory)
    try:
        return await asyncio.wait_for(
            aggregator.pnl_breakdown_snapshot(
                group_by=group_by,
                condition_id=condition_id,
                position_limit=position_limit,
            ),
            timeout=_PNL_BREAKDOWN_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, TimeoutError) as exc:
        logger.warning(
            "pnl_breakdown_snapshot timed out",
            extra={"group_by": group_by, "position_limit": position_limit},
        )
        raise HTTPException(
            status_code=504,
            detail={
                "reason": "pnl_breakdown_timeout",
                "timeout_s": _PNL_BREAKDOWN_TIMEOUT_SECONDS,
                "hint": "reduce position_limit or narrow filters",
            },
        ) from exc
    except ValueError as exc:
        # ValueError 文本可能含 schema 字段；§10 可审计性要求 trace_id 兜底反查。
        trace_id = uuid4().hex
        logger.warning(
            "pnl_breakdown validation failed",
            extra={"trace_id": trace_id, "error": str(exc), "group_by": group_by},
        )
        raise HTTPException(
            status_code=422,
            detail={"reason": "pnl_breakdown_validation_failed", "trace_id": trace_id},
        ) from exc
