from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from polymarket_trader.api.deps import build_time_range, get_admin_service
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
