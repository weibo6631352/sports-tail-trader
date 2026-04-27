from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from polymarket_trader.domain.events import DomainEventType, OutboxEvent

_USER_EVENT_TYPES = {
    DomainEventType.BALANCE_UPDATED.value,
    DomainEventType.ORDER_STATE_UPDATED.value,
    DomainEventType.FILL_RECORDED.value,
    DomainEventType.POSITION_UPDATED.value,
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
    if event_type_text not in _USER_EVENT_TYPES:
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
