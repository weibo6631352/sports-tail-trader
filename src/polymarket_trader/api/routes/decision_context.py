from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from polymarket_trader.api.aggregators import DecisionContextAggregator
from polymarket_trader.api.aggregators.decision_context_aggregator import (
    DEFAULT_AUDIT_CHANNELS,
)
from polymarket_trader.api.deps import build_time_range, get_runtime

router = APIRouter(tags=["decision-context"])


@router.get("/decision-context/{condition_id}")
async def get_decision_context(
    condition_id: str,
    token_id: str | None = Query(default=None, min_length=1),
    include_audit: bool = Query(
        default=False,
        description="是否附加 recent_audit_events——默认 False 零 DB 给前端高频刷,复盘场景显式 true 走 1 次 DB",
    ),
    audit_channels: str | None = Query(
        default=None,
        description="仅 include_audit=true 生效——逗号分隔 event_title 列表,缺省 = 复盘默认 7 类",
    ),
    audit_per_channel_limit: int = Query(default=20, ge=1, le=200),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """单 condition 完整决策上下文一次拉齐。

    返回 market(详情) + positions(全 outcomes 或单指定) + live_state(goalserve
    inplay/livescore)。默认纯内存零 DB,适合前端高频刷新。

    ``?include_audit=true`` 时附加 recent_audit_events(by_channel
    + chronological)走 1 次 DB——复盘场景使用。
    """

    if audit_channels:
        ch_tuple = tuple(c.strip() for c in audit_channels.split(",") if c.strip())
        if not ch_tuple:
            ch_tuple = DEFAULT_AUDIT_CHANNELS
    else:
        ch_tuple = DEFAULT_AUDIT_CHANNELS

    aggregator = DecisionContextAggregator(
        runtime=runtime, session_factory=runtime.db_session_factory
    )
    payload = await aggregator.get_context(
        condition_id=condition_id,
        token_id=token_id,
        include_audit=include_audit,
        audit_channels=ch_tuple,
        audit_per_channel_limit=audit_per_channel_limit,
        time_range=build_time_range(since=since, until=until),
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="market_not_found")
    return payload
