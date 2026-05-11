from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.deps import build_time_range, get_admin_service
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/sports", tags=["sports"])


@router.get("/live-events")
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
