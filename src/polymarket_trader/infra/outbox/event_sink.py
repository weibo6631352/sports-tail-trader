from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from polymarket_trader.domain.events import DomainEventType, OutboxEvent

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
    event_type = getattr(event, "event_type", None)
    event_id = getattr(event, "event_id", None)
    trace_id = getattr(event, "trace_id", None)
    if event_type is None or event_id is None or trace_id is None:
        return None
    event_type_text = str(event_type).strip()
    if event_type_text not in _PERSISTABLE_EVENT_TYPES:
        return None
    payload = getattr(event, "payload", {})
    if not isinstance(payload, Mapping):
        payload = {}
    return OutboxEvent(
        trace_id=str(trace_id),
        event_type=event_type_text,
        idempotency_key=str(event_id),
        event_id=str(event_id),
        market_slug=_text(getattr(event, "market_slug", None)),
        event_slug=_text(getattr(event, "event_slug", None)),
        condition_id=_text(getattr(event, "condition_id", None)),
        token_id=_text(getattr(event, "token_id", None)),
        reason=_text(getattr(event, "reason", None)),
        created_at=_datetime(getattr(event, "created_at", None)),
        priority=priority,
        payload=_project_payload(event_type_text, payload),
    )


def _project_payload(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if event_type == DomainEventType.BALANCE_UPDATED.value:
        projected: dict[str, Any] = {}
        for key in (
            "balance_usdc",
            "allowance_usdc",
            "user_ws_connected",
            "allow_new_entries",
            "market_pauses",
            "last_reconcile_at",
        ):
            if key in payload:
                projected[key] = payload[key]
        return projected
    if event_type == DomainEventType.ORDER_STATE_UPDATED.value:
        order_projected: dict[str, Any] = {}
        order = _mapping(payload, "order")
        fill = _mapping(payload, "fill")
        if order is not None:
            order_projected["order"] = order
        if fill is not None:
            order_projected["fill"] = fill
        return order_projected
    if event_type == DomainEventType.FILL_RECORDED.value:
        fill = _mapping(payload, "fill")
        return {} if fill is None else {"fill": fill}
    if event_type == DomainEventType.POSITION_UPDATED.value:
        position = _mapping(payload, "position")
        positions = _mapping_list(payload, "positions")
        projected = {}
        if position is not None:
            projected["position"] = position
        if positions:
            projected["positions"] = positions
        return projected
    if event_type == DomainEventType.SPORTS_LIVE_STATE_RECORDED.value:
        sports_projected: dict[str, Any] = {}
        for key in (
            "source",
            "observed_at",
            "signal_allowed",
            "signal_reason",
            "phase",
            "live_state_payload",
            "match_payload",
        ):
            if key in payload:
                sports_projected[key] = payload[key]
        return sports_projected
    if event_type == DomainEventType.ALLOCATION_DECISION_RECORDED.value:
        alloc_projected: dict[str, Any] = {}
        for key in (
            "candidates",
            "selected_condition_ids",
            "skipped_reasons",
            "total_budget_usdc",
            "buy_budget_usdc",
            "allocator",
        ):
            if key in payload:
                alloc_projected[key] = payload[key]
        return alloc_projected
    if event_type == DomainEventType.RISK_REJECTION_RECORDED.value:
        risk_projected: dict[str, Any] = {}
        for key in (
            "passed",
            "reason",
            "failed_field",
            "checks",
            "intent_summary",
            "decision_kind",
        ):
            if key in payload:
                risk_projected[key] = payload[key]
        return risk_projected
    if event_type == DomainEventType.MARKET_SETTLED.value:
        settle_projected: dict[str, Any] = {}
        for key in (
            "winning_token_id",
            "winning_outcome",
            "settled_at",
            "source",
            "payout_per_share",
            "fair_value_at_close",
        ):
            if key in payload:
                settle_projected[key] = payload[key]
        return settle_projected
    if event_type == DomainEventType.PARAMETER_OVERRIDE_APPLIED.value:
        param_projected: dict[str, Any] = {}
        for key in (
            "scope",
            "key",
            "previous_value",
            "new_value",
            "operator",
            "applied_at",
            "expires_at",
            "cleared",
        ):
            if key in payload:
                param_projected[key] = payload[key]
        return param_projected
    if event_type in _MARKET_EVENT_TYPES:
        # discovery 事件的 raw_market 是完整 Gamma payload（~每条数百字节），
        # 落库会让 audit 表暴涨。这里只保留 records.py 真正消费的字段：
        # - audit reason 已经在 OutboxEvent.reason 上；
        # - market_record builder 需要 market / tracked_market 快照；
        # - funnel/分析需要 discovery_kind / accepted 这类轻量元信息。
        projected: dict[str, Any] = {}
        market = _mapping(payload, "market", "market_snapshot")
        if market is not None:
            projected["market"] = market
        tracked_market = _mapping(payload, "tracked_market")
        if tracked_market is not None:
            projected["tracked_market"] = tracked_market
        for key in ("source", "summary", "accepted", "discovery_kind",
                    "extension_reason", "parse_status", "parse_reason",
                    "matched_keywords", "discovered_at", "strategy_id"):
            if key in payload:
                projected[key] = payload[key]
        return projected
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
