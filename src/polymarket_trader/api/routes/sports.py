from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Any

from polymarket_trader.api.deps import build_time_range, get_admin_service, get_runtime
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(tags=["sports"])


@router.get("/sports/live-events")
async def list_sports_live_events_history(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    condition_id: str | None = Query(default=None, min_length=1),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """历史体育实时事件——决策瞬间的比分/时钟/赛况快照。

    audit_events 中 ``event_title='sports_live_state_recorded'`` 的事件，
    payload 含 ``source / observed_at / signal_allowed / signal_reason /
    phase / match_payload``。复盘"为什么 t=00:00:12 决定入场"必备。
    """

    return await service.list_sports_live_events_history(
        limit=limit,
        offset=offset,
        condition_id=condition_id,
        time_range=build_time_range(since=since, until=until),
    )


@router.get("/sports/live-states")
async def list_sports_live_states(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """当前 EntryMetadataStore 中所有有直播状态的市场快照。

    每条记录含 ``live_state_payload``（含 ``goalserve_moneyline`` 赔率、比分、
    时钟等）+ ``condition_id / market_slug``，供前端渲染买入机会详情。
    """

    return await service.list_sports_live_states(limit=limit, offset=offset)


@router.get("/sports/source-gaps")
async def list_sports_live_source_gaps(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    prefix: str | None = Query(default=None, min_length=1),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """已跟踪市场中缺少 Goalserve 直播覆盖的缺口诊断。

    ``urgency`` 字段越高表示该市场越接近收盘但仍无直播状态。
    用于监控 Goalserve 覆盖率。
    """

    return await service.list_sports_live_source_gaps(limit=limit, offset=offset, prefix=prefix)


@router.get("/sports/live-states/{condition_id}")
async def get_sports_live_state(
    condition_id: str,
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """获取单个市场的 Goalserve 直播状态快照（含实时赔率）。

    ``live_state_payload.goalserve_moneyline`` 含 home/away 欧赔和隐含概率；
    ``live_state_payload.live_game`` 含比分、时钟、分节信息。
    市场无直播状态则 404。
    """

    result = await service.list_sports_live_states(limit=10000, offset=0)
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
    """当前 Goalserve 赛前赔率快照（内存状态）。

    不传 sport 时返回所有运动的 worker 状态摘要 + 各运动 match 数量。
    传 sport 时返回该运动分页后的 match 列表（含赔率）；每个运动可含数千条记录，
    默认返回前 100 条，用 limit/offset 翻页。
    worker 未启用时返回 disabled 状态。
    """

    worker = runtime.pregame_worker
    if worker is None:
        return {"enabled": False, "reason": "goalserve_pregame_not_configured"}

    status = worker.status_snapshot()
    if sport is not None:
        snapshot = worker.get_snapshot(sport)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="pregame_snapshot_not_found_for_sport")
        page = snapshot.matches[offset: offset + limit]
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


@router.get("/admin/series/state")
async def admin_series_state(
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """列出当前 ``EntryMetadataStore`` 中所有 series_state 快照。

    用于线上验证 ``series_state_worker`` 是否正常刷新比分；空 store 返回空数组
    （非 404），便于运维区分"worker 没跑"与"还没有 series 市场"。
    """

    return await service.list_series_state_snapshots(limit=limit, offset=offset)


@router.get("/admin/outright/team-resolution")
async def admin_outright_team_resolution(
    condition_id: str | None = Query(default=None, min_length=1),
    market_slug: str | None = Query(default=None, min_length=1),
    service: AdminService = Depends(get_admin_service),
) -> dict[str, object]:
    """诊断 outright 二元市场的球队解析过程。

    必须传 ``condition_id`` 或 ``market_slug`` 之一；market 找不到 → 404；
    market 存在但 metadata 里没有 season_odds 快照 → 200 + ``trace=None`` +
    ``reason='missing_season_odds'``（让前端区分"没数据"与"已解析"）；
    其余情形返回详细 trace，包含 normalized_text / candidate_teams / matches。
    """

    if not condition_id and not market_slug:
        raise HTTPException(
            status_code=400,
            detail="condition_id_or_market_slug_required",
        )
    payload = await service.outright_team_resolution(
        condition_id=condition_id,
        market_slug=market_slug,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="market_not_found")
    return payload
