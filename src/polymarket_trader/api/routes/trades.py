from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.aggregators import TimelineAggregator
from polymarket_trader.api.deps import build_time_range, get_runtime
from polymarket_trader.api.middleware.rate_limit import rate_limit

router = APIRouter(prefix="/trades", tags=["trades"])


@router.get("/{condition_id}/timeline")
async def get_trade_timeline(
    condition_id: str,
    token_id: str | None = Query(default=None, min_length=1),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
    per_table_limit: int = Query(default=1000, ge=1, le=5000),
    runtime: Any = Depends(get_runtime),
    _rate: None = Depends(rate_limit(endpoint="trade_timeline", qps=2.0, burst=5)),
) -> dict[str, object]:
    """单笔交易/单个市场全生命周期 timeline。"""

    aggregator = TimelineAggregator(
        session_factory=runtime.db_session_factory, runtime=runtime
    )
    return await aggregator.get_trade_timeline(
        condition_id=condition_id,
        token_id=token_id,
        time_range=build_time_range(since=since, until=until),
        limit=limit,
        per_table_limit=per_table_limit,
    )
