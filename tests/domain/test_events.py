from __future__ import annotations

from datetime import datetime, timezone

import pytest

from polymarket_trader.domain.events import AuditEvent, OutboxEvent, sanitize_raw_response
from polymarket_trader.infra.outbox import OutboxEvent as InfraOutboxEvent


def test_canonical_event_classes_are_shared_across_modules() -> None:
    assert InfraOutboxEvent is OutboxEvent


def test_audit_event_normalizes_utc_and_redacts_sensitive_fields() -> None:
    event = AuditEvent(
        event_title="order_submitted",
        trace_id="trace",
        created_at=datetime(2026, 1, 1, 12, 0, 0),
        updated_at=datetime(2026, 1, 1, 11, 59, 59),
        raw_response={
            "authorization": "Bearer abc123",
            "nested": {"api_key": "secret-value"},
        },
    )

    assert event.created_at.tzinfo == timezone.utc
    assert event.updated_at == event.created_at
    assert event.raw_response is not None
    assert "abc123" not in event.raw_response
    assert "secret-value" not in event.raw_response
    assert "[REDACTED]" in event.raw_response


def test_audit_event_does_not_promote_payload_raw_response() -> None:
    event = AuditEvent(
        event_title="order_submitted",
        trace_id="trace",
        payload={"raw_response": {"api_key": "secret-value"}},
    )

    assert event.raw_response is None
    assert event.payload["raw_response"] == {"api_key": "[REDACTED]"}


def test_audit_event_accepts_iso_timestamp_strings() -> None:
    event = AuditEvent(
        event_title="order_submitted",
        trace_id="trace",
        created_at="2026-01-01T12:00:00Z",
        updated_at="2026-01-01T12:00:01+00:00",
    )

    assert event.created_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert event.updated_at == datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc)


def test_outbox_event_accepts_iso_timestamp_string() -> None:
    event = OutboxEvent(
        trace_id="trace",
        event_type="market_discovered",
        idempotency_key="idem-1",
        created_at="2026-01-01T12:00:00Z",
    )

    assert event.created_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_market_events_carry_event_slug_as_first_class_field() -> None:
    audit = AuditEvent(
        event_title="market_discovered",
        trace_id="trace",
        market_slug="sample-market-a",
        event_slug="sample-event-a",
    )
    outbox = OutboxEvent(
        trace_id="trace",
        event_type="market_discovered",
        idempotency_key="idem-1",
        market_slug="sample-market-a",
        event_slug="sample-event-a",
    )

    assert audit.event_slug == "sample-event-a"
    assert audit.to_dict()["event_slug"] == "sample-event-a"
    assert audit.to_payload()["event_slug"] == "sample-event-a"
    assert outbox.event_slug == "sample-event-a"


def test_audit_event_requires_event_title_without_special_case_field_handling() -> None:
    with pytest.raises(ValueError, match="requires event_title"):
        AuditEvent(event_type="order_submitted", trace_id="trace")


def test_sanitize_raw_response_limits_and_redacts() -> None:
    summary = sanitize_raw_response({"api_key": "secret", "value": "ok"}, max_length=64)

    assert summary is not None
    assert "secret" not in summary
    assert "[REDACTED]" in summary
