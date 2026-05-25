from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from polymarket_trader.domain.events import AuditEvent, DomainEventType, OutboxEvent
from polymarket_trader.serialization import jsonable

_MARKET_EVENT_TYPES = {
    DomainEventType.MARKET_DISCOVERED.value,
    DomainEventType.MARKET_UPDATED.value,
    DomainEventType.MARKET_FILTERED_IN.value,
    DomainEventType.MARKET_FILTERED_OUT.value,
    DomainEventType.MARKET_RESOLVED_OR_DISABLED.value,
}
_ACCOUNT_EVENT_TYPES = {DomainEventType.BALANCE_UPDATED.value}
_ORDERBOOK_EVENT_TYPES = {
    DomainEventType.ORDERBOOK.value,
    DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED.value,
}
_ORDER_EVENT_TYPES = {
    DomainEventType.ORDER_CREATED.value,
    DomainEventType.ORDER_SIGNED.value,
    DomainEventType.ORDER_SUBMITTED.value,
    DomainEventType.ORDER_REJECTED.value,
    DomainEventType.ORDER_MATCHED.value,
    DomainEventType.ORDER_NO_FILL.value,
    DomainEventType.ORDER_PARTIALLY_FILLED.value,
    DomainEventType.ORDER_STATE_UPDATED.value,
    DomainEventType.ORDER_CANCEL_REQUESTED.value,
    DomainEventType.ORDER_CANCELLED.value,
    DomainEventType.REPLACE_ORDER_SUBMITTED.value,
    DomainEventType.UNEXPECTED_RESTING_ORDER_DETECTED.value,
}
_FILL_EVENT_TYPES = {
    DomainEventType.FILL_RECORDED.value,
    DomainEventType.TRADE_MINED.value,
    DomainEventType.TRADE_CONFIRMED.value,
}
_POSITION_EVENT_TYPES = {DomainEventType.POSITION_UPDATED.value}
_DECISION_EVENT_TYPES = {DomainEventType.DECISION_RECORDED.value}


@dataclass(frozen=True, slots=True)
class PersistencePlannedRecord:
    event: OutboxEvent
    kind: str
    record: Mapping[str, Any]


class PersistenceRecordBuilder:
    """事件落库记录构造器。

    strategy_id 在 main.py 设置 worker 时按 ``polymarket_trader.quant.identity.STRATEGY_ID`` 注入，
    单进程内值固定。Builder 在构造每条记录时把它统一盖上：
    生产者已经在事件 payload 里塞了 strategy_id，则优先使用 payload 中的值
    （便于未来同进程多策略），否则用 builder 持有的默认值。
    """

    def __init__(self, *, strategy_id: str) -> None:
        if not strategy_id:
            raise ValueError("PersistenceRecordBuilder requires non-empty strategy_id")
        self._strategy_id = strategy_id

    def _strategy_id_for(self, payload: Mapping[str, Any]) -> str:
        value = _first(payload, "strategy_id")
        if value is None:
            return self._strategy_id
        text = str(value).strip()
        return text or self._strategy_id

    def build_planned_records(
        self,
        events: Sequence[OutboxEvent],
    ) -> tuple[list[PersistencePlannedRecord], dict[str, int]]:
        planned_records: list[PersistencePlannedRecord] = []
        required_record_counts: dict[str, int] = {}
        for event in events:
            event_records = self.route_event(event)
            required_record_counts[event.event_id] = len(event_records)
            for kind, record in event_records:
                planned_records.append(PersistencePlannedRecord(event=event, kind=kind, record=record))
        return planned_records, required_record_counts

    def route_event(self, event: OutboxEvent) -> list[tuple[str, Mapping[str, Any]]]:
        records: list[tuple[str, Mapping[str, Any]]] = []
        event_type = _event_type_text(event)
        payload = dict(event.payload)

        # DECISION_RECORDED 是策略 hook 录制，专写 ``decision_records`` 表；
        # 它的 input/output payload 体积可观且语义与 audit / order 完全不重叠，
        # 因此独立成 kind，不再投到 audit_events，避免 audit 表被复盘数据稀释。
        if event_type in _DECISION_EVENT_TYPES:
            decision_record = self._build_decision_record(event, payload)
            if decision_record is not None:
                records.append(("decision", decision_record))
            records.append(("outbox", self._build_outbox_record(event, payload)))
            return records

        # RECONCILE_STARTED 跳过 audit:supervisor heartbeat 已记 reconcile 调度起点;
        # outbox_events 仍写,admin /reconcile_decisions?include_started=true 走
        # outbox 查询不受影响.每 20s 1 条 audit_events 表无审计价值.
        if event_type != DomainEventType.RECONCILE_STARTED.value:
            records.append(("audit", self._build_audit_record(event, payload)))
        for record in self._build_allocation_records(event, payload):
            records.append(("allocation", record))

        if event_type in _MARKET_EVENT_TYPES:
            market_record = self._build_market_record(event, payload)
            if market_record is not None:
                records.append(("market", market_record))
        if event_type in _ACCOUNT_EVENT_TYPES:
            records.append(("account", self._build_account_record(event, payload)))
        if event_type in _ORDERBOOK_EVENT_TYPES:
            orderbook_record = self._build_orderbook_record(event, payload)
            if orderbook_record is not None:
                records.append(("orderbook", orderbook_record))
        if event_type in _ORDER_EVENT_TYPES:
            order_record = self._build_order_record(event, payload)
            if order_record is not None:
                records.append(("order", order_record))
        if event_type in _FILL_EVENT_TYPES:
            records.extend(("fill", record) for record in self._build_fill_records(event, payload))
        if event_type in _POSITION_EVENT_TYPES:
            records.extend(("position", record) for record in self._build_position_records(event, payload))

        records.append(("outbox", self._build_outbox_record(event, payload)))
        return records

    def _build_decision_record(
        self,
        event: OutboxEvent,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        record_id = _first(payload, "record_id") or event.event_id
        if record_id is None:
            return None
        return {
            "idempotency_key": _kind_idempotency_key("decision", event),
            "record_id": str(record_id),
            "strategy_id": self._strategy_id_for(payload),
            "trace_id": event.trace_id,
            "hook_name": _first(payload, "hook_name"),
            "condition_id": event.condition_id or _first(payload, "condition_id"),
            "token_id": event.token_id or _first(payload, "token_id"),
            "market_slug": event.market_slug or _first(payload, "market_slug"),
            "decision_input": jsonable(_first(payload, "decision_input") or {}),
            "decision_output": jsonable(_first(payload, "decision_output") or {}),
            "accepted": bool(_first(payload, "accepted") or False),
            "reason": event.reason if event.reason is not None else _first(payload, "reason"),
            "created_at": jsonable(event.created_at),
        }

    def _build_audit_record(self, event: OutboxEvent, payload: Mapping[str, Any]) -> dict[str, Any]:
        audit_payload = dict(payload)
        audit_payload.update(
            {
                "idempotency_key": event.idempotency_key,
                "priority": event.priority,
                "retry_count": event.retry_count,
            }
        )
        audit = AuditEvent(
            event_title=str(event.event_type),
            trace_id=event.trace_id,
            strategy_id=self._strategy_id_for(payload),
            event_id=event.event_id,
            market_slug=event.market_slug,
            event_slug=event.event_slug,
            condition_id=event.condition_id,
            token_id=event.token_id,
            reason=event.reason or "",
            created_at=event.created_at,
            raw_response=event.raw_response_summary,
            payload=audit_payload,
        )
        record = audit.to_payload()
        record["idempotency_key"] = _kind_idempotency_key("audit", event)
        return jsonable(record)

    def _build_market_record(self, event: OutboxEvent, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        market = _mapping(payload, "market", "market_snapshot", "tracked_market")
        if market is None:
            return None
        fees = _mapping(market or {}, "fees") or {}

        record = _base_meta(event)
        record.update(
            {
                "idempotency_key": _kind_idempotency_key("market", event),
                "source": _first(payload, "source"),
                "discovery_kind": _first(payload, "discovery_kind"),
                "parse_status": _first(payload, "parse_status"),
                "parse_reason": _first(payload, "parse_reason"),
                "parse_detail": _first(payload, "parse_detail"),
                "matched_fields": jsonable(_first(payload, "matched_fields")),
                "matched_keywords": jsonable(_first(payload, "matched_keywords")),
                "accepted": _first(payload, "accepted"),
                "fees_enabled": _first(fees, "enabled"),
                "maker_base_fee_bps": _first(fees, "maker_base_fee_bps"),
                "taker_base_fee_bps": _first(fees, "taker_base_fee_bps"),
                "fee_rate_bps": _first(fees, "fee_rate_bps"),
                "fee_rate_updated_at": _first(fees, "fee_rate_updated_at"),
            }
        )
        record.update(jsonable(market))
        return record

    def _build_account_record(self, event: OutboxEvent, payload: Mapping[str, Any]) -> dict[str, Any]:
        record = _base_meta(event)
        account = {
            "account_key": "primary",
            "balance_usdc": _first(payload, "balance_usdc"),
            "allowance_usdc": _first(payload, "allowance_usdc"),
            "user_ws_connected": _first(payload, "user_ws_connected"),
            "allow_new_entries": _first(payload, "allow_new_entries"),
            "market_pauses": _first(payload, "market_pauses"),
            "last_reconcile_at": _first(payload, "last_reconcile_at"),
        }
        record.update(
            {
                "idempotency_key": _kind_idempotency_key("account", event),
            }
        )
        record.update(jsonable(account))
        return record

    def _build_orderbook_record(self, event: OutboxEvent, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        snapshot = _mapping(payload, "snapshot", "orderbook", "book")
        if snapshot is None:
            return None

        record = _base_meta(event)
        record.update(
            {
                "idempotency_key": _kind_idempotency_key("orderbook", event),
                "source": _first(payload, "source"),
                "snapshot_time": _first(payload, "snapshot_time"),
                "needs_rest_snapshot": _first(payload, "needs_rest_snapshot"),
                "spread": _first(payload, "spread"),
                "buyable_no_depth": _first(payload, "buyable_no_depth"),
            }
        )
        record.update(jsonable(snapshot))
        return record

    def _build_order_record(self, event: OutboxEvent, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        order = _mapping(payload, "order", "order_result", "intent")
        if order is None:
            order = _nested_mapping(payload, ("review", "order_result"), ("execution", "order_result"))
        if order is None:
            return None

        record = _base_meta(event)
        record.update(
            {
                "idempotency_key": _kind_idempotency_key("order", event),
            }
        )
        record.update(jsonable(order))
        # 强制覆盖：order payload 自带 strategy_id 时优先用它，否则用 builder 默认。
        # 不允许 strategy_id 为空写入 DB。
        record["strategy_id"] = _first(record, "strategy_id") or self._strategy_id_for(payload)
        return record

    def _build_fill_records(self, event: OutboxEvent, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        fills = _mapping_list(payload, "fill", "fills")
        if not fills:
            return []
        strategy_id = self._strategy_id_for(payload)
        return [_indexed_record(event, "fill", index, fill, strategy_id) for index, fill in enumerate(fills)]

    def _build_position_records(self, event: OutboxEvent, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        positions = _mapping_list(payload, "position", "positions")
        if not positions:
            return []
        strategy_id = self._strategy_id_for(payload)
        return [_indexed_record(event, "position", index, item, strategy_id) for index, item in enumerate(positions)]

    def _build_allocation_records(self, event: OutboxEvent, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        allocation = _mapping(payload, "allocation")
        allocations = _mapping_list(payload, "allocations")
        plan = _mapping(payload, "allocation_plan")

        if allocation is None and allocations:
            records_source = allocations
        elif allocation is not None:
            records_source = [allocation]
        elif plan is not None and isinstance(plan.get("allocations"), list):
            records_source = [dict(item) for item in plan["allocations"] if isinstance(item, Mapping)]
        else:
            return []

        strategy_id = self._strategy_id_for(payload)
        records: list[dict[str, Any]] = []
        for index, allocation_item in enumerate(records_source):
            record = _base_meta(event)
            record.update(
                {
                    "idempotency_key": _kind_idempotency_key("allocation", event),
                    "allocation_index": index,
                }
            )
            record.update(jsonable(allocation_item))
            record["strategy_id"] = _first(record, "strategy_id") or strategy_id
            records.append(record)
        return records

    def _build_outbox_record(self, event: OutboxEvent, payload: Mapping[str, Any]) -> dict[str, Any]:
        record = _base_meta(event)
        record.update(
            {
                "idempotency_key": event.idempotency_key,
                "payload": jsonable(payload),
            }
        )
        return record


def route_key(event: OutboxEvent) -> str:
    if event.merge_key:
        return event.merge_key
    parts = [
        str(event.event_type),
        event.market_slug or "",
        event.condition_id or "",
        event.token_id or "",
    ]
    return "|".join(parts)


def _base_meta(event: OutboxEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "trace_id": event.trace_id,
        "event_type": str(event.event_type),
        "market_slug": event.market_slug,
        "event_slug": event.event_slug,
        "condition_id": event.condition_id,
        "token_id": event.token_id,
        "reason": event.reason,
        "created_at": jsonable(event.created_at),
        "priority": event.priority,
        "retry_count": event.retry_count,
        "last_error": event.last_error,
        "idempotency_key": event.idempotency_key,
    }


def _indexed_record(
    event: OutboxEvent,
    kind: str,
    index: int,
    item: Mapping[str, Any],
    default_strategy_id: str,
) -> dict[str, Any]:
    record = _base_meta(event)
    record.update(
        {
            "idempotency_key": _kind_idempotency_key(kind, event),
            f"{kind}_index": index,
        }
    )
    record.update(jsonable(item))
    record["strategy_id"] = _first(record, "strategy_id") or default_strategy_id
    return record


def _kind_idempotency_key(kind: str, event: OutboxEvent) -> str:
    return f"{kind}:{event.idempotency_key}"


def _event_type_text(event: OutboxEvent) -> str:
    return str(event.event_type).strip()


def _first(payload: Mapping[str, Any], *keys: str) -> Any | None:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _mapping(payload: Mapping[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _nested_mapping(payload: Mapping[str, Any], *paths: tuple[str, str]) -> dict[str, Any] | None:
    for parent_key, child_key in paths:
        parent = payload.get(parent_key)
        if not isinstance(parent, Mapping):
            continue
        value = parent.get(child_key)
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _mapping_list(payload: Mapping[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return [dict(value)]
        if isinstance(value, list):
            records = [dict(item) for item in value if isinstance(item, Mapping)]
            if records:
                return records
    return []
