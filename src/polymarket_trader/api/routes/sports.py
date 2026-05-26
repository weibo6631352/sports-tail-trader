from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

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


@router.get("/sports/live-states/{condition_id}")
async def get_sports_live_state(
    condition_id: str,
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """获取单个市场的 Goalserve 直播状态快照（含实时赔率）。"""

    aggregator = SportsQueryAggregator(runtime=runtime)
    result = await aggregator.list_sports_live_states(limit=10000, offset=0)
    records = result.get("items", [])
    for record in records:
        if record.get("condition_id") == condition_id:
            return record
    raise HTTPException(status_code=404, detail="no_live_state_for_market")


@router.get("/sports/pregame/snapshot")
async def get_pregame_snapshot(
    sport: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=100, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """当前 Goalserve 赛前赔率快照（内存状态）。"""

    worker = runtime.pregame_worker
    if worker is None:
        return {"enabled": False, "reason": "goalserve_pregame_not_configured"}

    status = worker.status_snapshot()
    if sport is not None:
        snapshot = worker.get_snapshot(sport)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="pregame_snapshot_not_found_for_sport")
        page = snapshot.matches[offset : offset + limit]
        return {
            "status": status,
            "sport": snapshot.sport,
            "fetched_at": snapshot.fetched_at.isoformat(),
            "ts": snapshot.ts,
            "total": len(snapshot.matches),
            "offset": offset,
            "limit": limit,
            "matches": [
                {
                    "match_id": m.match_id,
                    "home_team": m.home_team,
                    "away_team": m.away_team,
                    "league": m.league,
                    "start_time": m.start_time.isoformat() if m.start_time else None,
                    "markets": [
                        {
                            "name": mk.name,
                            "suspended": mk.suspended,
                            "outcomes": [
                                {
                                    "name": o.name,
                                    "value_eu": str(o.value_eu),
                                    "implied_prob": str(o.implied_prob),
                                    "handicap": o.handicap,
                                    "suspended": o.suspended,
                                }
                                for o in mk.outcomes
                            ],
                        }
                        for mk in m.markets
                    ],
                }
                for m in page
            ],
        }
    snapshots = worker.get_snapshots()
    return {
        "status": status,
        "sports": {
            s: {
                "fetched_at": snap.fetched_at.isoformat(),
                "ts": snap.ts,
                "match_count": len(snap.matches),
            }
            for s, snap in snapshots.items()
        },
    }


@router.get("/operator/outright/team-resolution")
async def operator_outright_team_resolution(
    condition_id: str | None = Query(default=None, min_length=1),
    market_slug: str | None = Query(default=None, min_length=1),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """诊断 outright 二元市场的球队解析过程。"""

    if not condition_id and not market_slug:
        raise HTTPException(
            status_code=400,
            detail="condition_id_or_market_slug_required",
        )
    aggregator = SportsQueryAggregator(runtime=runtime)
    payload = await aggregator.outright_team_resolution(
        condition_id=condition_id,
        market_slug=market_slug,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="market_not_found")
    return payload
