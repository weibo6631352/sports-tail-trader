from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from polymarket_trader.api.deps import get_admin_service, get_runtime
from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.observability import HealthReporter, render_prometheus

router = APIRouter(tags=["health"])


def _reporter(runtime: Any) -> HealthReporter:
    return HealthReporter(runtime)


@router.get("/health")
async def health(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """总体健康（docs/新架构方案.md §11.4）—— 5 维度 status 聚合。

    机器可读：status ∈ {healthy, degraded, unhealthy}，detail 列各维度独立 status。
    任一 unhealthy → unhealthy；任一 degraded → degraded。
    """
    report = _reporter(runtime).overall()
    return {"status": report.status, "detail": report.detail}


@router.get("/health/live_sources")
async def health_live_sources(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """每个 (provider, sport) bucket 健康——stale > 60s → degraded；feeder FAILED → unhealthy。"""
    report = _reporter(runtime).live_sources()
    return {"status": report.status, "detail": report.detail}


@router.get("/health/ws")
async def health_ws(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """market_ws + user_ws 连接健康——connected=False or 30s 无消息 → degraded。"""
    report = _reporter(runtime).ws()
    return {"status": report.status, "detail": report.detail}


@router.get("/health/decision")
async def health_decision(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """event_bus 队列深度——超过 warn 阈值 → degraded。"""
    report = _reporter(runtime).decision()
    return {"status": report.status, "detail": report.detail}


@router.get("/health/account")
async def health_account(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """账户健康——balance<0 → unhealthy；last_reconcile > 300s → degraded。"""
    report = _reporter(runtime).account()
    return {"status": report.status, "detail": report.detail}


@router.get("/ready")
async def ready(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return service.readiness_snapshot()


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(runtime: Any = Depends(get_runtime)) -> str:
    """Prometheus exposition format —— 外部监控系统直接 scrape 此 endpoint。

    docs/新架构方案.md §11.4。MetricsRegistry.snapshot() 转 text/plain 格式，
    所有 counter / gauge / histogram 自动暴露。ObservabilityBridge 5s 周期写入
    的 13+ 个 gauge（audit dedupe / garbage / live source / account）也在内。
    """
    snapshot = runtime.metrics.snapshot()
    return render_prometheus(snapshot)
