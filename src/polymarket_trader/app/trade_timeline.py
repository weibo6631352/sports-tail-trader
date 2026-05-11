"""单笔交易/单个市场全生命周期 timeline 聚合（只读）。

Trade timeline 把分散在 ``decision_records`` / ``orders`` / ``fills`` /
``audit_events`` / ``outbox_events`` 的事件按时间戳合并成一条流，给操盘
和复盘用：

- 操盘：实时看一个 ``condition_id`` 上正在发生什么
- 复盘：事后追"discovery → 决策 → 风控 → 下单 → 成交 → reconcile → 平仓"
  的完整链条

不引入新的领域真相——只是对已有 DB 行的时间序合并 + 标准化投影。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from polymarket_trader.app.admin_serialization import AdminSerializer, decimal_text, jsonable
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent, Fill, OutboxEvent
from polymarket_trader.domain.order import Order
from polymarket_trader.domain.position import Position


@dataclass(frozen=True, slots=True)
class TradeTimelineInputs:
    decisions: Sequence[DecisionRecord]
    orders: Sequence[Order]
    fills: Sequence[Fill]
    audit_events: Sequence[AuditEvent]
    outbox_events: Sequence[OutboxEvent]
    positions: Sequence[Position]


def build_trade_timeline(
    *,
    condition_id: str,
    token_id: str | None,
    inputs: TradeTimelineInputs,
    serializer: AdminSerializer,
    limit: int = 1000,
) -> dict[str, Any]:
    """合并 timeline 事件，按 ``timestamp`` 升序返回。

    ``token_id`` 为 None 时聚合整个 ``condition_id`` 下所有 outcome；带
    ``token_id`` 时只看该 outcome 的事件。``limit`` 控制返回的事件数量上限，
    超出按时间倒序裁剪——避免长尾市场拉爆响应体。
    """

    events: list[dict[str, Any]] = []

    for record in inputs.decisions:
        if record.condition_id != condition_id:
            continue
        if token_id is not None and record.token_id is not None and record.token_id != token_id:
            continue
        events.append(_decision_event(record))

    for order in inputs.orders:
        if order.condition_id != condition_id:
            continue
        if token_id is not None and order.token_id != token_id:
            continue
        events.append(_order_event(order, serializer))

    for fill in inputs.fills:
        if fill.condition_id != condition_id:
            continue
        if token_id is not None and fill.token_id is not None and fill.token_id != token_id:
            continue
        events.append(_fill_event(fill, serializer))

    for audit in inputs.audit_events:
        if audit.condition_id != condition_id:
            continue
        if token_id is not None and audit.token_id is not None and audit.token_id != token_id:
            continue
        events.append(_audit_event(audit, serializer))

    for outbox in inputs.outbox_events:
        if outbox.condition_id != condition_id:
            continue
        if token_id is not None and outbox.token_id is not None and outbox.token_id != token_id:
            continue
        events.append(_outbox_event(outbox, serializer))

    events.sort(key=lambda ev: ev.get("timestamp") or "")
    truncated = False
    if len(events) > limit:
        # 超额时保留最近 limit 条——复盘最常关心"末端发生了什么"。
        events = events[-limit:]
        truncated = True

    final_position: dict[str, Any] | None = None
    for position in inputs.positions:
        if position.condition_id != condition_id:
            continue
        if token_id is not None and position.token_id != token_id:
            continue
        final_position = serializer.position(position)
        break

    return {
        "condition_id": condition_id,
        "token_id": token_id,
        "event_count": len(events),
        "truncated": truncated,
        "events": events,
        "current_position": final_position,
    }


def _decision_event(record: DecisionRecord) -> dict[str, Any]:
    return {
        "kind": "decision",
        "timestamp": jsonable(record.created_at),
        "hook_name": record.hook_name or None,
        "accepted": record.accepted,
        "reason": record.reason,
        "trace_id": record.trace_id,
        "token_id": record.token_id,
        "record_id": record.record_id,
        "decision_input": dict(record.decision_input),
        "decision_output": dict(record.decision_output),
    }


def _order_event(order: Order, serializer: AdminSerializer) -> dict[str, Any]:
    return {
        "kind": "order",
        "timestamp": jsonable(order.updated_at or order.created_at),
        "trace_id": order.trace_id,
        "token_id": order.token_id,
        "order_id": order.order_id,
        "trade_id": order.trade_id,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "status": order.status.value,
        "price": decimal_text(order.price),
        "filled_shares": decimal_text(order.filled_shares),
        "remaining_shares": decimal_text(order.remaining_shares),
        "notional_usdc": decimal_text(order.notional_usdc),
        "reason": order.reason,
        "full": serializer.order(order),
    }


def _fill_event(fill: Fill, serializer: AdminSerializer) -> dict[str, Any]:
    timestamp = fill.confirmed_at or fill.created_at
    return {
        "kind": "fill",
        "timestamp": jsonable(timestamp),
        "trace_id": fill.trace_id,
        "token_id": fill.token_id,
        "order_id": fill.order_id,
        "trade_id": fill.trade_id,
        "side": str(fill.side or "").lower() or None,
        "price": decimal_text(fill.price),
        "size": decimal_text(fill.size),
        "notional_usdc": decimal_text(fill.notional_usdc),
        "event_type": fill.event_type,
        "full": serializer.fill(fill),
    }


def _audit_event(event: AuditEvent, serializer: AdminSerializer) -> dict[str, Any]:
    return {
        "kind": "audit",
        "timestamp": jsonable(event.created_at),
        "trace_id": event.trace_id,
        "token_id": event.token_id,
        "event_title": event.event_title,
        "status": event.status,
        "reason": event.reason,
        "full": serializer.audit_event(event),
    }


def _outbox_event(event: OutboxEvent, serializer: AdminSerializer) -> dict[str, Any]:
    return {
        "kind": "outbox",
        "timestamp": jsonable(event.created_at),
        "trace_id": event.trace_id,
        "token_id": event.token_id,
        "event_type": event.event_type,
        "reason": event.reason,
        "retry_count": event.retry_count,
        "last_error": event.last_error,
        "full": serializer.outbox_event(event),
    }


def _event_sort_timestamp(ev: dict[str, Any]) -> datetime | None:
    ts = ev.get("timestamp")
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
