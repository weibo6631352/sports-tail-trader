"""Analytics 只读接口。

提供漏斗、拒绝原因 top、执行质量三类聚合查询。
不在路由层执行业务规则；service 注入由 app.state.analytics_service 提供。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from polymarket_trader.api.deps import build_time_range, get_admin_service
from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.app.analytics_service import AnalyticsService

# 默认窗口：24h；本地常量，不进 Settings。
DEFAULT_WINDOW_MS: int = 86_400_000

# 上限保护：最长 7 天，避免误传超大窗口拖垮 DB。
MAX_WINDOW_MS: int = 7 * 24 * 60 * 60 * 1000

router = APIRouter(prefix="/analytics", tags=["analytics"])


def get_analytics_service(request: Request) -> AnalyticsService:
    provider = getattr(request.app.state, "get_analytics_service", None)
    if callable(provider):
        service = provider()
        if service is not None:
            return service
    service = getattr(request.app.state, "analytics_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="analytics_service_unavailable")
    return service


@router.get("/funnel")
async def get_funnel(
    window_ms: int = Query(default=DEFAULT_WINDOW_MS, gt=0, le=MAX_WINDOW_MS),
    end_ms: int | None = Query(default=None, ge=0),
    league: str | None = Query(default=None, min_length=1, max_length=64),
    market_type: str | None = Query(default=None, min_length=1, max_length=64),
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
    service: AnalyticsService = Depends(get_analytics_service),
) -> dict[str, Any]:
    return await service.funnel(
        window_ms=window_ms,
        end_ms=end_ms,
        league=league,
        market_type=market_type,
        strategy_id=strategy_id,
    )


@router.get("/rejections")
async def get_rejections(
    window_ms: int = Query(default=DEFAULT_WINDOW_MS, gt=0, le=MAX_WINDOW_MS),
    end_ms: int | None = Query(default=None, ge=0),
    league: str | None = Query(default=None, min_length=1, max_length=64),
    market_type: str | None = Query(default=None, min_length=1, max_length=64),
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
    service: AnalyticsService = Depends(get_analytics_service),
) -> dict[str, Any]:
    return await service.rejections(
        window_ms=window_ms,
        end_ms=end_ms,
        league=league,
        market_type=market_type,
        limit=20,
        strategy_id=strategy_id,
    )


@router.get("/execution-quality")
async def get_execution_quality(
    window_ms: int = Query(default=DEFAULT_WINDOW_MS, gt=0, le=MAX_WINDOW_MS),
    end_ms: int | None = Query(default=None, ge=0),
    league: str | None = Query(default=None, min_length=1, max_length=64),
    market_type: str | None = Query(default=None, min_length=1, max_length=64),
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
    service: AnalyticsService = Depends(get_analytics_service),
) -> dict[str, Any]:
    return await service.execution_quality(
        window_ms=window_ms,
        end_ms=end_ms,
        league=league,
        market_type=market_type,
        strategy_id=strategy_id,
    )


@router.get("/edge-realization")
async def get_edge_realization(
    limit: int = Query(default=200, ge=1, le=2000),
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
    condition_id: str | None = Query(default=None, min_length=1),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, Any]:
    """Edge 实现度：预测 edge vs 实际 per-share 回报。

    对每个 ``accepted=true`` 的决策，从 ``decision_output`` 抽 ``fair_value`` 和
    ``entry_price``，按 ``(condition_id, token_id)`` 找仓位算 ``realized_pnl /
    cost`` （已平仓优先）或 ``cash_pnl / cost`` （未平仓），最后按预测 edge
    分桶（``0-50/50-100/100-200/200-500/500-1000/>1000bps``）给均值/中位数/
    胜率。判断"定价模型对不对"最直接的指标。
    """

    return await service.edge_realization_snapshot(
        limit=limit,
        strategy_id=strategy_id,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/risk-rejections")
async def list_risk_rejections(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    condition_id: str | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, Any]:
    """风控结构化拒绝详情——逐条 ``check_name / failed_field / value /
    suggested_action`` 列表（与 ``/analytics/rejections`` 的 reason 字符串
    top 聚合互补）。"""

    return await service.list_risk_rejections(
        limit=limit,
        offset=offset,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/risk-rejections/aggregate")
async def aggregate_risk_rejections(
    sample_limit: int = Query(default=1000, ge=1, le=5000),
    condition_id: str | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, Any]:
    """按 ``check_name`` 和 ``failed_field`` 聚合风控拒绝——"哪条风控规则
    在拒哪类市场"的真相。"""

    return await service.aggregate_risk_rejections(
        sample_limit=sample_limit,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/calibration")
async def get_calibration(
    bucket_size: float = Query(default=0.05, gt=0.0, le=0.5),
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    sample_limit: int = Query(default=2000, ge=1, le=10000),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, Any]:
    """定价模型校准 + Brier score / log-loss。

    对每个 ``accepted=true`` 决策按 ``decision_output.fair_value`` 分桶；
    每桶在市场结算后统计实际命中率。Brier = ``mean((fair - outcome)^2)``，
    越接近 0 校准越好。
    """

    from decimal import Decimal

    return await service.calibration_snapshot(
        bucket_size=Decimal(str(bucket_size)),
        strategy_id=strategy_id,
        time_range=build_time_range(since=since, until=until),
        sample_limit=sample_limit,
    )


@router.get("/missed-opportunities")
async def get_missed_opportunities(
    limit: int = Query(default=500, ge=1, le=5000),
    per_decision_usdc: float = Query(default=10.0, gt=0, le=10_000),
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, Any]:
    """被风控/策略拒绝的决策事后盈利模拟。

    join 决策与 settlement 事件，按 reason 聚合"如果当时下了会赚还是亏"。
    配合 ``/analytics/risk-rejections`` 用——判断风控阈值是否过严。
    """

    from decimal import Decimal

    return await service.missed_opportunities_snapshot(
        limit=limit,
        per_decision_usdc=Decimal(str(per_decision_usdc)),
        strategy_id=strategy_id,
        time_range=build_time_range(since=since, until=until),
    )


__all__ = ("router", "DEFAULT_WINDOW_MS", "MAX_WINDOW_MS", "get_analytics_service")
