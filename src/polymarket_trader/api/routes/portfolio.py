from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.app.portfolio_history_service import (
    DEFAULT_INTERVAL_MS,
    DEFAULT_WINDOW_MS,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

# 单次 pnl_breakdown 上限——20000 positions × markets join 在退化数据下可能跑数十秒，
# 不能让 admin 查询长期占用 DB 连接（§7 不阻塞 P0）。超时改为 504 + 提示降 limit。
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
async def get_portfolio(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return await service.portfolio_snapshot()


@router.get("/exposure")
async def get_portfolio_exposure(
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """每市场未平仓名义暴露（纯内存快照，零 DB，零 P0 影响）。

    返回所有持仓按市场分解的：名义市值、浮动盈亏、平均入场价、
    当前价、挂单预留资金、暂停状态。是操盘者"我的风险在哪里"的全局视图。
    """

    return await service.portfolio_exposure()


@router.get("/equity-curve")
async def get_equity_curve(
    window_ms: int = Query(default=DEFAULT_WINDOW_MS, ge=1),
    interval_ms: int = Query(default=DEFAULT_INTERVAL_MS, ge=1),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    try:
        return await service.portfolio_equity_curve(
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
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """组合级风险归因。

    复用 equity-curve 的 downsampled 时间序列，计算 max drawdown / time
    underwater / 波动率 / Sharpe-like / total return。``annualization_factor``
    可选，例如按 1d 桶传 365 来年化 Sharpe。
    """

    try:
        return await service.portfolio_risk_metrics(
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
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """按维度分解的 PnL 聚合。

    回答"哪个 strategy / market / category / outcome 是赚钱主力，哪个在烧钱"。
    ``category`` 和 ``outcome`` 维度会做一次 markets 批量 join；其他维度直接
    走 positions 表，零 join 成本。
    """

    try:
        return await asyncio.wait_for(
            service.pnl_breakdown_snapshot(
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
