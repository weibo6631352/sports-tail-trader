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
