from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from polymarket_trader.api.aggregators import TimelineAggregator
from polymarket_trader.api.deps import build_time_range, get_runtime

router = APIRouter(prefix="/audit-events", tags=["audit-events"])


@router.get("")
async def list_audit_events(
    # 上限 500 → 5000: reconcile 每 30s 产 2 条,500 条只覆盖 ~4h 复盘,
    # 跨多事件类型聚合时被 reconcile 占满。5000 给操盘复盘足够空间。
    limit: int = Query(default=100, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    trace_id: str | None = Query(default=None),
    event_title: str | None = Query(default=None),
    condition_id: str | None = Query(default=None),
    token_id: str | None = Query(default=None),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    # 默认 False:大表 count subquery 每次 ~870ms,monitor 高频拉取不需要 total.
    include_total: bool = Query(default=False),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    aggregator = TimelineAggregator(
        session_factory=runtime.db_session_factory, runtime=runtime
    )
    return await aggregator.list_audit_events(
        limit=limit,
        offset=offset,
        trace_id=trace_id,
        event_title=event_title,
        condition_id=condition_id,
        token_id=token_id,
        time_range=build_time_range(since=since, until=until),
        with_total=include_total,
    )


# 默认 channels：仅列实际写入 audit_events 表的 event_title。
# sports_live_state_recorded → 不入 audit_events(outbox_sink 显式排除),
#   查询走 /sports/live-events?condition_id={cid} 的内存 ring buffer
# allocation_decision_recorded → 同上,查询走 WS candidates 实时流或 decision_records
#   (/decision-context/{cid}?include_audit=true)
# 把它们留在默认列表里只会让前端永远拿到空 channel,违反"接口返回的数据要合理".
_DEFAULT_BY_CONDITION_CHANNELS: tuple[str, ...] = (
    "order_created",
    "order_matched",
    "order_rejected",
    "fill_recorded",
    "risk_rejection_recorded",
    "reconcile_diff_detected",
    "reconcile_applied",
    "trading_paused_for_market",
)


@router.get("/by-condition/{condition_id}")
async def get_audit_events_by_condition(
    condition_id: str,
    channels: str | None = Query(
        default=None,
        description="逗号分隔的 event_title 列表，缺省 = 复盘默认 7 类",
    ),
    per_channel_limit: int = Query(default=50, ge=1, le=500),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """单 condition 多 channel 审计事件时间线——一次拉齐复盘 §15 四步骤。

    避免 agent/操盘连发 N 次 ``/audit-events?event_title=...&condition_id=...``。
    单 SQL 命中 ``ix_audit_events_condition_id`` + ``ix_audit_events_event_title``
    复合过滤；返回 ``by_channel`` 分组视图 + ``chronological`` 统一时间序列。

    只查 ``audit_events`` 表内事件。如需其它复盘数据:
    - 直播状态时间线 → ``GET /sports/live-events?condition_id={cid}``
    - 决策评估时间线 → ``GET /decision-context/{cid}?include_audit=true``
      或 WS ``candidates`` channel 实时流
    """

    if channels:
        ch_tuple = tuple(c.strip() for c in channels.split(",") if c.strip())
        if not ch_tuple:
            ch_tuple = _DEFAULT_BY_CONDITION_CHANNELS
    else:
        ch_tuple = _DEFAULT_BY_CONDITION_CHANNELS

    aggregator = TimelineAggregator(
        session_factory=runtime.db_session_factory, runtime=runtime
    )
    return await aggregator.get_audit_events_by_condition(
        condition_id=condition_id,
        channels=ch_tuple,
        time_range=build_time_range(since=since, until=until),
        per_channel_limit=per_channel_limit,
    )


@router.get("/operators")
async def aggregate_operator_interventions(
    operator: str | None = Query(default=None, min_length=1, max_length=64),
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    sample_limit: int = Query(default=2000, ge=1, le=10_000),
    runtime: Any = Depends(get_runtime),
) -> dict[str, object]:
    """人工干预审计聚合——按 operator 分组事件计数 + 类型分布。"""

    aggregator = TimelineAggregator(
        session_factory=runtime.db_session_factory, runtime=runtime
    )
    return await aggregator.aggregate_operator_interventions(
        operator=operator,
        time_range=build_time_range(since=since, until=until),
        sample_limit=sample_limit,
    )
