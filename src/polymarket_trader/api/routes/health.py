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
    """总体健康（原架构方案 §11.4）—— 5 维度 status 聚合。

    机器可读：status ∈ {healthy, degraded, unhealthy}，detail 列各维度独立 status。
    任一 unhealthy → unhealthy；任一 degraded → degraded。
    """
    report = _reporter(runtime).overall()
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
