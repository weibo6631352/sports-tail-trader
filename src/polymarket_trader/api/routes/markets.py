from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.deps import build_time_range, get_admin_service
from polymarket_trader.api.rate_limit import rate_limit
from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.infra.polymarket import PolymarketClientError

router = APIRouter(prefix="/markets", tags=["markets"])


class PauseMarketRequest(BaseModel):
    condition_id: str = Field(min_length=1)
    reason: str = Field(default="manual_pause", min_length=1)
    operator: str = "manual"
    # 前端 confirmAction 生成；后端 audit chain 串"操作意图 + 持久事件"。
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


class ResumeMarketRequest(BaseModel):
    condition_id: str = Field(min_length=1)
    operator: str = "manual"
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


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


@router.get("/orderbook-history")
async def list_orderbook_history(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    token_id: str | None = Query(default=None, min_length=1),
    condition_id: str | None = Query(default=None, min_length=1),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """历史盘口快照查询，按 ``received_at`` 倒序。

    ``orderbook_snapshots`` 表已经在落，本接口只暴露 GET。复盘"入场那一秒
    的盘口"用——按 token_id / condition_id + 时间窗过滤。
    """

    if token_id is None and condition_id is None:
        raise HTTPException(status_code=422, detail="token_id_or_condition_id_required")
    return await service.list_orderbook_history(
        limit=limit,
        offset=offset,
        token_id=token_id,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/prices-history")
async def get_market_prices_history(
    token_id: str = Query(min_length=1),
    start_ts: int | None = Query(default=None, ge=0),
    end_ts: int | None = Query(default=None, ge=0),
    interval: Literal["max", "all", "1m", "1w", "1d", "6h", "1h"] | None = Query(default=None),
    fidelity: int | None = Query(default=None, ge=1),
    service: AdminService = Depends(get_admin_service),
    _rate: None = Depends(rate_limit(endpoint="prices_history", qps=2.0, burst=5)),
) -> dict[str, object]:
    if interval is None and start_ts is None:
        raise HTTPException(
            status_code=422,
            detail="prices_history_requires_time_filter: provide interval or start_ts",
        )
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
        trace_id=request.trace_id,
    )


@router.post("/resume")
async def resume_market(
    request: ResumeMarketRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    return await service.resume_market_manual(
        condition_id=request.condition_id,
        operator=request.operator,
        trace_id=request.trace_id,
    )


class SettleMarketRequest(BaseModel):
    condition_id: str = Field(min_length=1)
    winning_token_id: str = Field(min_length=1)
    winning_outcome: str | None = None
    source: str = Field(default="manual")
    operator: str = "manual"


@router.get("/{condition_id}/settlement")
async def get_market_settlement(
    condition_id: str,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """单市场最新 settlement——含 winning_token_id / outcome / 时间戳，加上
    我们最后一次 accepted 决策的 fair_value 偏差（``outcome - fair``，正数
    表示低估了赢家）。"""

    payload = await service.get_market_settlement(condition_id=condition_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="settlement_not_found")
    return payload


@router.get("/settlements")
async def list_market_settlements(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    condition_id: str | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """市场结算历史——按 ``event_title='market_settled'`` 投影。"""

    return await service.list_market_settlements(
        limit=limit,
        offset=offset,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )


@router.post("/settle")
async def settle_market(
    request: SettleMarketRequest,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """手工记录市场结算结果。

    目前没有自动 settlement 抓取链路；此接口让运维在确认 outcome 后写入
    ``market_settled`` 事件，给 calibration / Brier score 提供 ground truth。
    payload 含 ``winning_token_id`` / ``winning_outcome`` / ``source`` /
    ``operator``。
    """

    return await service.record_market_settlement(
        condition_id=request.condition_id,
        winning_token_id=request.winning_token_id,
        winning_outcome=request.winning_outcome,
        source=request.source,
        operator=request.operator,
    )
