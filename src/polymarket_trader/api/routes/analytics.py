"""Analytics 只读接口。

提供漏斗、拒绝原因 top、执行质量三类聚合查询。
不在路由层执行业务规则；service 注入由 app.state.analytics_service 提供。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from polymarket_trader.api.aggregators import AnalyticsAggregator
from polymarket_trader.api.deps import build_time_range, get_runtime
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
    service: AnalyticsService = Depends(get_analytics_service),
) -> dict[str, Any]:
    return await service.funnel(
        window_ms=window_ms,
        end_ms=end_ms,
        league=league,
        market_type=market_type,
    )


@router.get("/rejections")
async def get_rejections(
    window_ms: int = Query(default=DEFAULT_WINDOW_MS, gt=0, le=MAX_WINDOW_MS),
    end_ms: int | None = Query(default=None, ge=0),
    league: str | None = Query(default=None, min_length=1, max_length=64),
    market_type: str | None = Query(default=None, min_length=1, max_length=64),
    service: AnalyticsService = Depends(get_analytics_service),
) -> dict[str, Any]:
    return await service.rejections(
        window_ms=window_ms,
        end_ms=end_ms,
        league=league,
        market_type=market_type,
        limit=20,
    )


@router.get("/execution-quality")
async def get_execution_quality(
    window_ms: int = Query(default=DEFAULT_WINDOW_MS, gt=0, le=MAX_WINDOW_MS),
    end_ms: int | None = Query(default=None, ge=0),
    league: str | None = Query(default=None, min_length=1, max_length=64),
    market_type: str | None = Query(default=None, min_length=1, max_length=64),
    service: AnalyticsService = Depends(get_analytics_service),
) -> dict[str, Any]:
    return await service.execution_quality(
        window_ms=window_ms,
        end_ms=end_ms,
        league=league,
        market_type=market_type,
    )


@router.get("/edge-realization")
async def get_edge_realization(
    limit: int = Query(default=200, ge=1, le=2000),
    condition_id: str | None = Query(default=None, min_length=1),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """Edge 实现度：预测 edge vs 实际 per-share 回报。"""

    aggregator = AnalyticsAggregator(session_factory=runtime.db_session_factory)
    return await aggregator.edge_realization_snapshot(
        limit=limit,
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
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """风控结构化拒绝详情。"""

    aggregator = AnalyticsAggregator(session_factory=runtime.db_session_factory)
    return await aggregator.list_risk_rejections(
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
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """按 check_name 和 failed_field 聚合风控拒绝。"""

    aggregator = AnalyticsAggregator(session_factory=runtime.db_session_factory)
    return await aggregator.aggregate_risk_rejections(
        sample_limit=sample_limit,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/calibration")
async def get_calibration(
    bucket_size: float = Query(default=0.05, gt=0.0, le=0.5),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    sample_limit: int = Query(default=2000, ge=1, le=10000),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """定价模型校准 + Brier score / log-loss。"""

    from decimal import Decimal

    aggregator = AnalyticsAggregator(session_factory=runtime.db_session_factory)
    return await aggregator.calibration_snapshot(
        bucket_size=Decimal(str(bucket_size)),
        time_range=build_time_range(since=since, until=until),
        sample_limit=sample_limit,
    )


@router.get("/missed-opportunities")
async def get_missed_opportunities(
    limit: int = Query(default=500, ge=1, le=5000),
    per_decision_usdc: float = Query(default=10.0, gt=0, le=10_000),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, Any]:
    """被风控/策略拒绝的决策事后盈利模拟。"""

    from decimal import Decimal

    aggregator = AnalyticsAggregator(session_factory=runtime.db_session_factory)
    return await aggregator.missed_opportunities_snapshot(
        limit=limit,
        per_decision_usdc=Decimal(str(per_decision_usdc)),
        time_range=build_time_range(since=since, until=until),
    )


__all__ = ("router", "DEFAULT_WINDOW_MS", "MAX_WINDOW_MS", "get_analytics_service")
