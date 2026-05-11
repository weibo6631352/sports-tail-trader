from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.app.portfolio_history_service import (
    DEFAULT_INTERVAL_MS,
    DEFAULT_WINDOW_MS,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("")
async def get_portfolio(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return await service.portfolio_snapshot()


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
        raise HTTPException(status_code=503, detail=str(exc))


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
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/pnl-breakdown")
async def get_pnl_breakdown(
    group_by: str = Query(
        default="strategy_id",
        description="strategy_id | market_slug | condition_id | category | outcome | redeemable_status",
    ),
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
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
        return await service.pnl_breakdown_snapshot(
            group_by=group_by,
            strategy_id=strategy_id,
            condition_id=condition_id,
            position_limit=position_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
