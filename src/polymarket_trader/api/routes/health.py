from __future__ import annotations

from fastapi import APIRouter, Depends

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(service: AdminService = Depends(get_admin_service)) -> dict[str, str]:
    return service.health_snapshot()


@router.get("/ready")
async def ready(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return service.readiness_snapshot()
