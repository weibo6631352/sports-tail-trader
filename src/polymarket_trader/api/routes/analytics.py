"""Analytics 只读接口。

提供漏斗、拒绝原因 top、执行质量三类聚合查询。
不在路由层执行业务规则；service 注入由 app.state.analytics_service 提供。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

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


__all__ = ("router", "DEFAULT_WINDOW_MS", "MAX_WINDOW_MS", "get_analytics_service")
