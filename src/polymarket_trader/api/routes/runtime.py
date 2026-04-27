from __future__ import annotations

from fastapi import APIRouter, Depends

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(tags=["runtime"])


@router.get("/runtime")
async def runtime(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return await service.runtime_snapshot()


@router.get("/workers")
async def workers(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return service.workers_snapshot()


@router.get("/metrics")
async def metrics(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return service.metrics_snapshot()
