from __future__ import annotations

from fastapi import APIRouter, Depends

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("")
async def get_portfolio(service: AdminService = Depends(get_admin_service)) -> dict[str, object]:
    return await service.portfolio_snapshot()
