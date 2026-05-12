from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from polymarket_trader.domain.events import DomainEventType, EventEnvelope, OutboxEvent

logger = logging.getLogger(__name__)
# 未知 event_type 警告频次去重，避免 worker 循环刷屏。1024 个不同 type 是天花板。
_unknown_event_types_warned: set[str] = set()

# 这个白名单决定哪些事件可以落 audit / outbox 表。命名上不限于"用户态"——
# discovery 类事件也需要审计落库以满足 CLAUDE.md §10「拒绝原因必须可审计」，
# 同时给 /analytics/funnel 提供真实计数。Worker 侧已用 _seen_* 缓存做首次
# 拒绝才发的去重，避免每轮重复发现把 audit 淹没。
_PERSISTABLE_EVENT_TYPES = {
    DomainEventType.BALANCE_UPDATED.value,
    DomainEventType.ORDER_STATE_UPDATED.value,
    DomainEventType.FILL_RECORDED.value,
    DomainEventType.POSITION_UPDATED.value,
    DomainEventType.MARKET_DISCOVERED.value,
    DomainEventType.MARKET_UPDATED.value,
    DomainEventType.MARKET_FILTERED_IN.value,
    DomainEventType.MARKET_FILTERED_OUT.value,
    DomainEventType.MARKET_RESOLVED_OR_DISABLED.value,
    # Batch 3 落库：观测面板、分析面板与拒绝原因深挖刚需，全部走 audit_events
    # 通道（PersistenceRecordBuilder 默认 route 到 audit），单条 payload 体积
    # 受 _project_payload 截断。
    DomainEventType.SPORTS_LIVE_STATE_RECORDED.value,
    DomainEventType.ALLOCATION_DECISION_RECORDED.value,
    DomainEventType.RISK_REJECTION_RECORDED.value,
    DomainEventType.MARKET_SETTLED.value,
    DomainEventType.PARAMETER_OVERRIDE_APPLIED.value,
    # 主交易开关变更必须可审计——CLAUDE.md §10「拒绝、降级、恢复动作必须保留可审计原因」
    DomainEventType.TRADING_PAUSED.value,
    DomainEventType.TRADING_RESUMED.value,
    # cancel / replace 是人工 + 自动都会触发的写动作，事后查"谁/何时/为什么撤了这单"
    # 必须有独立 audit。ORDER_STATE_UPDATED 只记录结果，不记 intent + operator + reason。
    DomainEventType.ORDER_CANCEL_REQUESTED.value,
    DomainEventType.ORDER_CANCELLED.value,
    DomainEventType.REPLACE_ORDER_SUBMITTED.value,
    # Reconcile 与单市场暂停事件：admin_query.reconcile_decisions / timeline 都查这
    # 几种 type；不落 audit 会让 /admin/reconcile_diffs 永远返回 0，违反 §10 可审计要求。
    # RECONCILE_STARTED 是 admin opt-in（include_started=True）才查，但同样落库，
    # 否则跨"调度起点"维度的复盘缺失。
    DomainEventType.RECONCILE_STARTED.value,
    DomainEventType.RECONCILE_DIFF_DETECTED.value,
    DomainEventType.RECONCILE_APPLIED.value,
    DomainEventType.TRADING_PAUSED_FOR_MARKET.value,
}

_MARKET_EVENT_TYPES = {
    DomainEventType.MARKET_DISCOVERED.value,
    DomainEventType.MARKET_UPDATED.value,
    DomainEventType.MARKET_FILTERED_IN.value,
    DomainEventType.MARKET_FILTERED_OUT.value,
    DomainEventType.MARKET_RESOLVED_OR_DISABLED.value,
}


class OutboxSink(Protocol):
    def put_nowait(self, event: OutboxEvent) -> bool: ...


def build_domain_event_outbox_sink(outbox: OutboxSink) -> Callable[[int, Any], None]:
    def _sink(priority: int, event: Any) -> None:
        outbox_event = _to_outbox_event(priority, event)
        if outbox_event is None:
            return
        outbox.put_nowait(outbox_event)

    return _sink


def _to_outbox_event(priority: int, event: Any) -> OutboxEvent | None:
    if not isinstance(event, EventEnvelope):
        return None
    event_type_text = str(event.event_type).strip()
    if not event_type_text:
        return None
    if event_type_text not in _PERSISTABLE_EVENT_TYPES:
        # 上游新增 event_type 但忘改 _PERSISTABLE_EVENT_TYPES 会导致事件静默丢失，
        # 上线后只能事后才发现「audit 漏了」。这里记一次 warning（按 type 去重避免刷屏）。
        if event_type_text not in _unknown_event_types_warned:
            _unknown_event_types_warned.add(event_type_text)
            logger.warning(
                "outbox_sink: dropping non-persistable event_type=%s (add to _PERSISTABLE_EVENT_TYPES if audit needed)",
                event_type_text,
            )
        return None
    payload = getattr(event, "payload", {})
    if not isinstance(payload, Mapping):
        payload = {}
    return OutboxEvent(
        trace_id=event.trace_id,
        event_type=event_type_text,
        idempotency_key=event.event_id,
        event_id=event.event_id,
        market_slug=_text(event.market_slug),
        event_slug=_text(event.event_slug),
        condition_id=_text(event.condition_id),
        token_id=_text(event.token_id),
        reason=_text(event.reason),
        created_at=_datetime(event.created_at),
        priority=priority,
        payload=_project_payload(event_type_text, payload),
    )


# 字段集合型投影：取 payload 里若干已知字段做白名单透出，避免 audit 表暴涨。
# 多个 event_type 共享同一字段集时只定义一次（如 cancel/cancelled/replace 同
# _ORDER_ACTION_FIELDS）。新增 event 时只需在这里加一项，无需改 dispatch 函数。

_ORDER_ACTION_FIELDS = (
    "operator",
    "reason",
    "order_id",
    "trade_id",
    "old_price",
    "new_price",
    "old_size_shares",
    "new_size_shares",
    "side",
    "order_type",
    "result_status",
    "occurred_at",
)
_TRADING_TOGGLE_FIELDS = (
    "operator",
    "reason",
    "phase_before",
    "phase_after",
    "previous_manual_pause_reason",
    "degraded_reason",
    "occurred_at",
)
_MARKET_DISCOVERY_FIELDS = (
    "source",
    "summary",
    "accepted",
    "discovery_kind",
    "extension_reason",
    "parse_status",
    "parse_reason",
    "matched_keywords",
    "discovered_at",
    "strategy_id",
)

_FIELD_PROJECTIONS: dict[str, tuple[str, ...]] = {
    DomainEventType.BALANCE_UPDATED.value: (
        "balance_usdc",
        "allowance_usdc",
        "user_ws_connected",
        "allow_new_entries",
        "market_pauses",
        "last_reconcile_at",
    ),
    DomainEventType.SPORTS_LIVE_STATE_RECORDED.value: (
        "source",
        "observed_at",
        "signal_allowed",
        "signal_reason",
        "phase",
        "live_state_payload",
        "match_payload",
    ),
    DomainEventType.ALLOCATION_DECISION_RECORDED.value: (
        "candidates",
        "selected_condition_ids",
        "skipped_reasons",
        "total_budget_usdc",
        "buy_budget_usdc",
        "allocator",
    ),
    DomainEventType.RISK_REJECTION_RECORDED.value: (
        "passed",
        "reason",
        "failed_field",
        "checks",
        "intent_summary",
        "decision_kind",
    ),
    DomainEventType.MARKET_SETTLED.value: (
        "winning_token_id",
        "winning_outcome",
        "settled_at",
        "source",
        "payout_per_share",
        "fair_value_at_close",
    ),
    DomainEventType.PARAMETER_OVERRIDE_APPLIED.value: (
        "scope",
        "key",
        "previous_value",
        "new_value",
        "operator",
        "applied_at",
        "expires_at",
        "cleared",
    ),
    DomainEventType.ORDER_CANCEL_REQUESTED.value: _ORDER_ACTION_FIELDS,
    DomainEventType.ORDER_CANCELLED.value: _ORDER_ACTION_FIELDS,
    DomainEventType.REPLACE_ORDER_SUBMITTED.value: _ORDER_ACTION_FIELDS,
    DomainEventType.TRADING_PAUSED.value: _TRADING_TOGGLE_FIELDS,
    DomainEventType.TRADING_RESUMED.value: _TRADING_TOGGLE_FIELDS,
    DomainEventType.RECONCILE_STARTED.value: (
        "market_count",
        "paused_market_count",
        "diff_count",
        "trigger_event_type",
        "refresh_summary",
    ),
    DomainEventType.RECONCILE_DIFF_DETECTED.value: (
        "action_type",
        "source_order_id",
        "source_order_side",
        "target_size_shares",
        "target_notional_usdc",
        "pause_reason",
        "metadata",
    ),
    DomainEventType.RECONCILE_APPLIED.value: (
        "action_count",
        "applied_count",
        "failed_count",
    ),
    DomainEventType.TRADING_PAUSED_FOR_MARKET.value: (
        "market_status",
        "pause_reason",
    ),
}


def _pick_fields(payload: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: payload[key] for key in fields if key in payload}


def _project_order_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    """ORDER_STATE_UPDATED：保留 order + fill 子映射。其他大字段（raw_response 等）丢弃。"""
    projected: dict[str, Any] = {}
    order = _mapping(payload, "order")
    fill = _mapping(payload, "fill")
    if order is not None:
        projected["order"] = order
    if fill is not None:
        projected["fill"] = fill
    return projected


def _project_fill(payload: Mapping[str, Any]) -> dict[str, Any]:
    fill = _mapping(payload, "fill")
    return {} if fill is None else {"fill": fill}


def _project_position(payload: Mapping[str, Any]) -> dict[str, Any]:
    """POSITION_UPDATED：单 position 或 positions 列表，按需透出。"""
    projected: dict[str, Any] = {}
    position = _mapping(payload, "position")
    positions = _mapping_list(payload, "positions")
    if position is not None:
        projected["position"] = position
    if positions:
        projected["positions"] = positions
    return projected


def _project_market_discovery(payload: Mapping[str, Any]) -> dict[str, Any]:
    """MARKET_* 事件：discovery 的 raw_market 是完整 Gamma payload（每条数百字节），
    落库会让 audit 表暴涨。只保留 records.py 真正消费的字段：
    - audit reason 已经在 OutboxEvent.reason 上；
    - market_record builder 需要 market / tracked_market 快照；
    - funnel/分析需要 discovery_kind / accepted 这类轻量元信息。
    """
    projected: dict[str, Any] = {}
    market = _mapping(payload, "market", "market_snapshot")
    if market is not None:
        projected["market"] = market
    tracked_market = _mapping(payload, "tracked_market")
    if tracked_market is not None:
        projected["tracked_market"] = tracked_market
    projected.update(_pick_fields(payload, _MARKET_DISCOVERY_FIELDS))
    return projected


# 函数型投影：逻辑超出"取字段"的事件（需要 _mapping 解析、子结构判断）。
_FUNCTION_PROJECTIONS: dict[str, Any] = {
    DomainEventType.ORDER_STATE_UPDATED.value: _project_order_state,
    DomainEventType.FILL_RECORDED.value: _project_fill,
    DomainEventType.POSITION_UPDATED.value: _project_position,
}


def _project_payload(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    fn = _FUNCTION_PROJECTIONS.get(event_type)
    if fn is not None:
        return fn(payload)
    fields = _FIELD_PROJECTIONS.get(event_type)
    if fields is not None:
        return _pick_fields(payload, fields)
    if event_type in _MARKET_EVENT_TYPES:
        return _project_market_discovery(payload)
    return {}


def _datetime(value: Any | None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _mapping(payload: Mapping[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _mapping_list(payload: Mapping[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        items = [dict(item) for item in value if isinstance(item, Mapping)]
        if items:
            return items
    return []


def _text(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
