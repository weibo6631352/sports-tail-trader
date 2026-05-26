from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from polymarket_trader.api.aggregators import (
    MarketDetailAggregator,
    MarketMiscAggregator,
    SettlementAggregator,
)
from polymarket_trader.api.deps import build_time_range, get_admin_service, get_runtime
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
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    level: Literal["summary", "detail"] = Query("detail"),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """单 market 详情（基于 DataGraph MarketView，零 DB / 零外部 API 调用）。

    支持按 condition_id 或 token_id 查询。`level` 控制返回字段量。market_slug
    查询路径已不再支持（按 §12.6 不为前端兼容保留旧形态——前端按 cid/tid 查）。
    """
    if not (condition_id or token_id):
        raise HTTPException(status_code=422, detail="condition_id or token_id is required")
    aggregator = MarketDetailAggregator(data_graph=runtime.data_graph)
    if condition_id:
        payload = aggregator.detail(condition_id, level=level)
    else:
        # token_id → market_view_for_token
        view = runtime.data_graph.market_view_for_token(token_id)
        payload = None if view is None else aggregator.detail(view.condition_id, level=level)
    if payload is None:
        raise HTTPException(status_code=404, detail="market not found")
    return payload


@router.post("/batch")
async def get_markets_batch(
    condition_ids: list[str],
    level: Literal["summary", "detail"] = Query("summary"),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """批量 market 详情——避免 N 次单调（docs/新架构方案.md §12.3 ⑤）。"""
    aggregator = MarketDetailAggregator(data_graph=runtime.data_graph)
    items = aggregator.batch_detail(tuple(condition_ids), level=level)
    return {"markets": list(items), "count": len(items)}


@router.get("/orderbook")
async def get_market_orderbook(
    market_slug: str | None = Query(default=None),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    if token_id is None:
        raise HTTPException(status_code=422, detail="token_id is required")
    aggregator = MarketMiscAggregator(runtime=runtime)
    try:
        payload = await aggregator.get_market_orderbook(
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
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    if token_id is None:
        raise HTTPException(status_code=422, detail="token_id is required")
    aggregator = MarketMiscAggregator(runtime=runtime)
    try:
        payload = await aggregator.get_market_midpoint(
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


@router.get("/orderbook-direction")
async def get_orderbook_direction(
    token_id: str = Query(min_length=1),
    window_seconds: float | None = Query(default=None, ge=0.5, le=120.0),
    windows: str | None = Query(default=None, description="逗号分隔的多窗口秒数,如 2,5,10,30"),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """读盘口多时点 delta 信号(best bid/ask price + size 变化)。

    单时点 bid/ask 深度比会被 MM 远端"墙"骗;真买卖压来自窗口内 best 价位移 +
    size 消耗。返回 direction_score [-1,+1] + raw deltas + confidence + samples。
    每次查询落 audit(ORDERBOOK_DIRECTION_QUERIED),供事后复盘。

    用法:
    - 单窗口(向后兼容): ``?window_seconds=10`` → 返回扁平 signal dict
    - 多窗口: ``?windows=2,5,10,30`` → 返回 {token_id, signals: [...]} 一次查询比对各窗口
    - 都不传时默认 ``window_seconds=10``
    """
    if windows is not None:
        try:
            window_list = [float(w.strip()) for w in windows.split(",") if w.strip()]
        except ValueError:
            raise HTTPException(status_code=422, detail="windows must be comma-separated floats")
        if not window_list:
            raise HTTPException(status_code=422, detail="windows must contain at least one value")
        for w in window_list:
            if w < 0.5 or w > 120.0:
                raise HTTPException(status_code=422, detail=f"each window must be in [0.5, 120.0], got {w}")
        aggregator = MarketMiscAggregator(runtime=runtime, session_factory=runtime.db_session_factory)
        return await aggregator.get_orderbook_direction_multi(token_id=token_id, windows=tuple(window_list))
    aggregator = MarketMiscAggregator(runtime=runtime, session_factory=runtime.db_session_factory)
    return await aggregator.get_orderbook_direction(
        token_id=token_id, window_seconds=window_seconds if window_seconds is not None else 10.0,
    )


@router.get("/orderbook-depth")
async def get_orderbook_depth(
    token_id: str = Query(min_length=1),
    windows: str = Query(default="2,3,5,10", description="逗号分隔的窗口秒数"),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """完整盘口资金分布 + 我方 resting/持仓 + 多窗口波动。"""
    try:
        windows_s = tuple(float(w.strip()) for w in windows.split(",") if w.strip())
    except ValueError:
        raise HTTPException(status_code=422, detail="windows must be comma-separated floats")
    if not windows_s:
        windows_s = (2.0, 3.0, 5.0, 10.0)
    return MarketMiscAggregator(runtime=runtime).orderbook_depth_snapshot(
        token_id=token_id, windows_s=windows_s,
    )


@router.get("/liquidity-summary")
async def get_liquidity_summary(
    top_n: int = Query(default=20, ge=1, le=100),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """全市场盘口资金聚合 + 双边比例 + whale 大单 + by_classification + top N 深度。"""
    return MarketMiscAggregator(runtime=runtime).liquidity_summary_snapshot(top_n=top_n)


@router.get("/event-bundle")
async def get_event_bundle(
    event_slug: str = Query(min_length=1),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """一个 event 下所有 condition_id 的市场聚合视图（ML/Totals/Spreads/分节 prop 一次拿）。"""
    return MarketMiscAggregator(runtime=runtime).event_bundle_snapshot(event_slug=event_slug)


@router.get("/data-health")
async def get_market_data_health(
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """单市场所有数据源连接状态 + 新鲜度。"""
    if condition_id is None and token_id is None:
        raise HTTPException(status_code=422, detail="condition_id or token_id required")
    return MarketMiscAggregator(runtime=runtime).market_data_health_snapshot(
        condition_id=condition_id, token_id=token_id,
    )


@router.get("/orderbook-history")
async def list_orderbook_history(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    token_id: str | None = Query(default=None, min_length=1),
    condition_id: str | None = Query(default=None, min_length=1),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """历史盘口快照查询，按 ``received_at`` 倒序。"""
    if token_id is None and condition_id is None:
        raise HTTPException(status_code=422, detail="token_id_or_condition_id_required")
    return await MarketMiscAggregator(runtime=runtime).list_orderbook_history(
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
    runtime: Any = Depends(get_runtime),
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
        return await MarketMiscAggregator(runtime=runtime).get_market_prices_history(
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
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    return await MarketMiscAggregator(runtime=runtime).list_markets(
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
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """单市场最新 settlement（走 SettlementAggregator）— 含 winning_token_id /
    outcome / 时间戳，加上我们最后一次 accepted 决策的 fair_value 偏差
    （``outcome - fair``，正数表示低估了赢家）。"""

    aggregator = SettlementAggregator(session_factory=runtime.db_session_factory)
    payload = await aggregator.settlement_for(condition_id)
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
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """市场结算历史（走 SettlementAggregator）— 按 ``event_title='market_settled'`` 投影。"""

    aggregator = SettlementAggregator(session_factory=runtime.db_session_factory)
    return await aggregator.list_settlements(
        limit=limit,
        offset=offset,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/liquidity")
async def get_market_liquidity(
    token_id: str = Query(min_length=1),
    condition_id: str | None = Query(default=None),
    market_slug: str | None = Query(default=None),
    depth_ticks: int = Query(default=5, ge=1, le=20),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """盘口流动性快照（纯 WS 热缓存，零 DB，零 P0 影响）。"""
    payload = MarketMiscAggregator(runtime=runtime).get_market_liquidity(
        token_id=token_id,
        condition_id=condition_id,
        market_slug=market_slug,
        depth_ticks=depth_ticks,
    )
    if payload is None:
        raise HTTPException(status_code=503, detail="orderbook_snapshot_unavailable")
    return payload


@router.get("/impact")
async def get_market_impact(
    token_id: str = Query(min_length=1),
    size_usdc: float = Query(gt=0, le=100_000, description="目标买入 USDC 金额"),
    condition_id: str | None = Query(default=None),
    market_slug: str | None = Query(default=None),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """下单前冲击成本估算（纯 WS 热缓存，零 DB，零 P0 影响）。"""
    payload = MarketMiscAggregator(runtime=runtime).get_market_impact(
        token_id=token_id,
        size_usdc=Decimal(str(size_usdc)),
        condition_id=condition_id,
        market_slug=market_slug,
    )
    if payload is None:
        raise HTTPException(status_code=503, detail="orderbook_snapshot_unavailable")
    return payload


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
