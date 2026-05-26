from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from polymarket_trader.api.aggregators import RuntimeAggregator
from polymarket_trader.api.deps import get_runtime
from polymarket_trader.observability import HealthReporter, render_prometheus

router = APIRouter(tags=["health"])


def _reporter(runtime: Any) -> HealthReporter:
    return HealthReporter(runtime)


@router.get("/health")
async def health(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """总体健康（原架构方案 §11.4）—— 4 维度 status 聚合。

    机器可读：status ∈ {healthy, degraded, unhealthy}，detail 列各维度独立 status。
    任一 unhealthy → unhealthy；任一 degraded → degraded。

    具体维度详情走 ``/health/live-sources`` / ``/health/ws`` /
    ``/health/decision`` / ``/health/account``——返回独立 detail (含 bucket 列表 /
    idle_seconds / queue_depths / balance 等),用于具体定位.
    """
    report = _reporter(runtime).overall()
    return {"status": report.status, "detail": report.detail}


@router.get("/health/live-sources")
async def health_live_sources(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """直播源健康——bucket stale > 60s → degraded; feeder FAILED → unhealthy."""
    report = _reporter(runtime).live_sources()
    return {"status": report.status, "detail": report.detail}


@router.get("/health/ws")
async def health_ws(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """WS 连接健康——market_ws/user_ws 分维度 connected + idle 检查."""
    report = _reporter(runtime).ws()
    return {"status": report.status, "detail": report.detail}


@router.get("/health/decision")
async def health_decision(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """决策链路健康——trading_queue_depth > warn_depth → degraded."""
    report = _reporter(runtime).decision()
    return {"status": report.status, "detail": report.detail}


@router.get("/health/account")
async def health_account(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """账户状态健康——last_reconcile > 300s → degraded; balance < 0 → unhealthy."""
    report = _reporter(runtime).account()
    return {"status": report.status, "detail": report.detail}


@router.get("/ready")
async def ready(runtime: Any = Depends(get_runtime)) -> dict[str, object]:
    return RuntimeAggregator(runtime=runtime).readiness_snapshot()


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(runtime: Any = Depends(get_runtime)) -> str:
    """Prometheus exposition format —— 外部监控系统直接 scrape 此 endpoint。

    原架构方案 §11.4。MetricsRegistry.snapshot() 转 text/plain 格式，
    所有 counter / gauge / histogram 自动暴露。ObservabilityBridge 5s 周期写入
    的 13+ 个 gauge（audit dedupe / garbage / live source / account）也在内。
    """
    snapshot = runtime.metrics.snapshot()
    return render_prometheus(snapshot)
