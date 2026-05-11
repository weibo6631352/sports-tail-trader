from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import get_admin_service
from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.infra.polymarket import PolymarketClientError

router = APIRouter(prefix="/markets", tags=["markets"])


class PauseMarketRequest(BaseModel):
    condition_id: str = Field(min_length=1)
    reason: str = Field(default="manual_pause", min_length=1)
    operator: str = "manual"


class ResumeMarketRequest(BaseModel):
    condition_id: str = Field(min_length=1)
    operator: str = "manual"


@router.get("/detail")
async def get_market_detail(
    market_slug: str | None = Query(default=None),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    if not any((market_slug, condition_id, token_id)):
        raise HTTPException(status_code=422, detail="market_slug, condition_id, or token_id is required")
    payload = await service.get_market(
        market_slug=market_slug,
        condition_id=condition_id,
        token_id=token_id,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="market not found")
    return payload


@router.get("/orderbook")
async def get_market_orderbook(
    market_slug: str | None = Query(default=None),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    if token_id is None:
        raise HTTPException(status_code=422, detail="token_id is required")
    try:
        payload = await service.get_market_orderbook(
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
        )
    except PolymarketClientError as exc:
        raise HTTPException(status_code=502, detail="market_orderbook_upstream_unavailable") from exc
    except RuntimeError as exc:
        if str(exc) != "clob_client unavailable":
            raise
        raise HTTPException(status_code=503, detail="clob_client_unavailable") from exc
    if payload is None:
        raise HTTPException(status_code=404, detail="market not found")
    return payload


@router.get("/midpoint")
async def get_market_midpoint(
    market_slug: str | None = Query(default=None),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    if token_id is None:
        raise HTTPException(status_code=422, detail="token_id is required")
    try:
        payload = await service.get_market_midpoint(
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
        )
    except PolymarketClientError as exc:
        raise HTTPException(status_code=502, detail="market_midpoint_upstream_unavailable") from exc
    except RuntimeError as exc:
        if str(exc) != "clob_client unavailable":
            raise
        raise HTTPException(status_code=503, detail="clob_client_unavailable") from exc
    if payload is None:
        raise HTTPException(status_code=404, detail="market not found")
    return payload


@router.get("/prices-history")
async def get_market_prices_history(
    token_id: str = Query(min_length=1),
    start_ts: int | None = Query(default=None, ge=0),
    end_ts: int | None = Query(default=None, ge=0),
    interval: Literal["max", "all", "1m", "1w", "1d", "6h", "1h"] | None = Query(default=None),
    fidelity: int | None = Query(default=None, ge=1),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise HTTPException(status_code=422, detail="start_ts must be <= end_ts")
    try:
        return await service.get_market_prices_history(
            token_id=token_id,
            start_ts=start_ts,
            end_ts=end_ts,
            interval=interval,
            fidelity=fidelity,
        )
    except PolymarketClientError as exc:
        raise HTTPException(status_code=502, detail="market_prices_history_upstream_unavailable") from exc
    except RuntimeError as exc:
        if str(exc) != "clob_client unavailable":
            raise
        raise HTTPException(status_code=503, detail="clob_client_unavailable") from exc


@router.get("")
async def list_markets(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    trading_status: str | None = Query(default=None),
    fees_enabled: bool | None = Query(default=None),
    fee_rate_bps_min: int | None = Query(default=None, ge=0),
    fee_rate_bps_max: int | None = Query(default=None, ge=0),
    maker_base_fee_bps_min: int | None = Query(default=None, ge=0),
    maker_base_fee_bps_max: int | None = Query(default=None, ge=0),
    taker_base_fee_bps_min: int | None = Query(default=None, ge=0),
    taker_base_fee_bps_max: int | None = Query(default=None, ge=0),
    sort_by: Literal[
        "market_slug",
        "fee_rate_bps",
        "fee_rate_updated_at",
        "maker_base_fee_bps",
        "taker_base_fee_bps",
    ]
    | None = Query(default=None),
    sort_direction: Literal["asc", "desc"] = Query(default="desc"),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.list_markets(
        limit=limit,
        offset=offset,
        trading_status=trading_status,
        fees_enabled=fees_enabled,
        fee_rate_bps_min=fee_rate_bps_min,
        fee_rate_bps_max=fee_rate_bps_max,
        maker_base_fee_bps_min=maker_base_fee_bps_min,
        maker_base_fee_bps_max=maker_base_fee_bps_max,
        taker_base_fee_bps_min=taker_base_fee_bps_min,
        taker_base_fee_bps_max=taker_base_fee_bps_max,
        sort_by=sort_by,
        sort_direction=sort_direction,
    )


@router.post("/pause")
async def pause_market(
    request: PauseMarketRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.pause_market_manual(
        condition_id=request.condition_id,
        reason=request.reason,
        operator=request.operator,
    )


@router.post("/resume")
async def resume_market(
    request: ResumeMarketRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.resume_market_manual(
        condition_id=request.condition_id,
        operator=request.operator,
    )
