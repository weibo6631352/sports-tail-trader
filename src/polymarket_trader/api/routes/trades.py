from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.deps import build_time_range, get_admin_service
from polymarket_trader.api.rate_limit import rate_limit
from polymarket_trader.app.admin_service import AdminService

router = APIRouter(prefix="/trades", tags=["trades"])


@router.get("/{condition_id}/timeline")
async def get_trade_timeline(
    condition_id: str,
    token_id: str | None = Query(default=None, min_length=1),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
    per_table_limit: int = Query(default=1000, ge=1, le=5000),
    service: AdminService = Depends(get_admin_service),
    _rate: None = Depends(rate_limit(endpoint="trade_timeline", qps=2.0, burst=5)),
) -> dict[str, object]:
    """单笔交易/单个市场全生命周期 timeline。

    把 ``decision_records / orders / fills / audit_events / outbox_events``（含
    reconcile）按时间戳合并成一条流，外加当前 ``position`` 快照——操盘和复盘
    第一刚需。
    """

    return await service.get_trade_timeline(
        condition_id=condition_id,
        token_id=token_id,
        time_range=build_time_range(since=since, until=until),
        limit=limit,
        per_table_limit=per_table_limit,
    )
