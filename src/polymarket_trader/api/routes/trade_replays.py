from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.aggregators import TimelineAggregator
from polymarket_trader.api.deps import get_runtime

router = APIRouter(prefix="/trade-replays", tags=["trade-replays"])


@router.get("")
async def list_trade_replays(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    trace_id: str | None = Query(default=None),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    aggregator = TimelineAggregator(
        session_factory=runtime.db_session_factory, runtime=runtime
    )
    return await aggregator.list_trade_replays(
        limit=limit,
        offset=offset,
        condition_id=condition_id,
        token_id=token_id,
        trace_id=trace_id,
    )
