from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.aggregators import SportsQueryAggregator
from polymarket_trader.api.deps import build_time_range, get_runtime

router = APIRouter(tags=["sports"])


@router.get("/sports/live-events")
async def list_sports_live_events_history(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    condition_id: str | None = Query(default=None, min_length=1),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """历史体育实时事件——决策瞬间的比分/时钟/赛况快照。"""

    aggregator = SportsQueryAggregator(runtime=runtime)
    return await aggregator.list_sports_live_events_history(
        limit=limit,
        offset=offset,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/sports/live-states")
async def list_sports_live_states(
    limit: int = Query(default=5000, ge=1, le=20000),
    offset: int = Query(default=0, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """当前 MarketMetadataStore 中所有有直播状态的市场快照。"""

    aggregator = SportsQueryAggregator(runtime=runtime)
    return await aggregator.list_sports_live_states(limit=limit, offset=offset)


@router.get("/sports/source-gaps")
async def list_sports_live_source_gaps(
    limit: int = Query(default=5000, ge=1, le=20000),
    offset: int = Query(default=0, ge=0),
    prefix: str | None = Query(default=None, min_length=1),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """已跟踪市场中缺少 Goalserve 直播覆盖的缺口诊断。"""

    aggregator = SportsQueryAggregator(runtime=runtime)
    return await aggregator.list_sports_live_source_gaps(
        limit=limit, offset=offset, prefix=prefix
    )


