from __future__ import annotations

from polymarket_trader.app.admin_serialization import AdminSerializer
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import AuditEvent, OutboxEvent
from polymarket_trader.runtime.registry import MarketRegistrySnapshot
from polymarket_trader.workers.persistence_records import PersistenceRecordBuilder


def test_admin_audit_event_serialization_exposes_payload_for_candidate_replay() -> None:
    serializer = AdminSerializer(
        account_snapshot_provider=AccountSnapshot,
        registry_snapshot_provider=lambda: MarketRegistrySnapshot(()),
        market_ws_snapshot=lambda token_id: None,
    )
    event = AuditEvent(
        event_title="skipped",
        trace_id="trace-audit",
        payload={
            "plan_metadata": {
                "sports_tail_reason": "missing_live_game_state",
                "sports_tail_action": "reject",
            }
        },
    )

    payload = serializer.audit_event(event)

    assert payload["payload"]["plan_metadata"] == {
        "sports_tail_reason": "missing_live_game_state",
        "sports_tail_action": "reject",
    }


def test_persistence_audit_record_keeps_outbox_payload_for_candidate_replay() -> None:
    event = OutboxEvent(
        trace_id="trace-persistence",
        event_type="skipped",
        idempotency_key="audit:trace-persistence",
        payload={
            "plan_metadata": {
                "sports_tail_reason": "missing_live_game_state",
                "sports_tail_action": "reject",
            }
        },
    )

    records = PersistenceRecordBuilder().route_event(event)
    audit_record = next(record for kind, record in records if kind == "audit")

    assert audit_record["plan_metadata"] == {
        "sports_tail_reason": "missing_live_game_state",
        "sports_tail_action": "reject",
    }
