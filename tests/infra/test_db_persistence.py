from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.account import (
    AccountSnapshot,
    MarketPause,
    MarketPauseReason,
    MarketPauseSource,
)
from polymarket_trader.domain.events import DomainEventType, OutboxEvent
from polymarket_trader.domain.market import TradingStatus
from polymarket_trader.infra.db.models import AccountSnapshotModel, MarketModel
from polymarket_trader.infra.db.record_mappers import (
    audit_event_from_record,
    fill_from_record,
    market_from_record,
    outbox_event_from_record,
)
from polymarket_trader.workers.persistence_records import PersistenceRecordBuilder
from tests.helpers.markets import build_binary_market


def test_audit_event_from_record_restores_serialized_timestamps() -> None:
    event = audit_event_from_record(
        {
            "trace_id": "trace-1",
            "event_title": "market_filtered_out",
            "created_at": "2026-04-15T07:39:39.514588+00:00",
            "updated_at": "2026-04-15T07:39:39.600000+00:00",
        }
    )

    assert event is not None
    assert event.created_at == datetime(2026, 4, 15, 7, 39, 39, 514588, tzinfo=timezone.utc)
    assert event.updated_at == datetime(2026, 4, 15, 7, 39, 39, 600000, tzinfo=timezone.utc)
    assert event.payload["created_at"] == "2026-04-15 07:39:39.514588+00:00"
    assert event.payload["updated_at"] == "2026-04-15 07:39:39.600000+00:00"


def test_audit_event_from_record_restores_explicit_event_slug() -> None:
    event = audit_event_from_record(
        {
            "trace_id": "trace-1",
            "event_title": "market_discovered",
            "market_slug": "sample-market-a",
            "event_slug": "sample-event-a",
        }
    )

    assert event is not None
    assert event.event_slug == "sample-event-a"


def test_market_from_record_prefers_fee_schedule_rate_from_raw_payload() -> None:
    market = market_from_record(
        {
            "condition_id": "condition-1",
            "market_slug": "sample-market-a",
            "token_ids": ["yes-token", "no-token"],
            "outcomes": [
                {"token_id": "yes-token", "outcome": "YES"},
                {"token_id": "no-token", "outcome": "NO"},
            ],
            "tick_size": "0.01",
            "min_order_size": "1",
            "taker_base_fee_bps": 1000,
            "fee_rate_bps": 1000,
            "raw_payload": {
                "feeSchedule": {
                    "rate": "0.072",
                }
            },
        }
    )

    assert market is not None
    assert market.taker_base_fee_bps == 72
    assert market.fee_rate_bps == 72


def test_market_model_to_domain_prefers_fee_schedule_rate_from_raw_payload() -> None:
    model = MarketModel.from_domain(
        build_binary_market(
            condition_id="condition-1",
            market_slug="sample-market-a",
            no_token_id="no-token",
            yes_token_id="yes-token",
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("1"),
            taker_base_fee_bps=1000,
            fee_rate_bps=1000,
            trading_status=TradingStatus.ELIGIBLE,
        ),
        trace_id="trace-1",
        source="test",
        raw_payload={
            "feeSchedule": {
                "rate": "0.072",
            }
        },
    )

    market = model.to_domain()

    assert market.taker_base_fee_bps == 72
    assert market.fee_rate_bps == 72


def test_account_snapshot_model_preserves_structured_market_pause_roundtrip() -> None:
    pause = MarketPause.build(
        condition_id="condition-1",
        reason=MarketPauseReason.MARKET_NOT_TRADABLE,
        source=MarketPauseSource.RISK,
        recoverable=False,
    )
    snapshot = AccountSnapshot(
        balance_usdc=Decimal("1"),
        allowance_usdc=Decimal("1"),
        market_pauses=(pause,),
    )

    restored = AccountSnapshotModel.from_domain(snapshot).to_domain()

    assert restored.market_pauses == (pause,)


def test_market_persistence_worker_skips_market_wide_record_without_structured_market() -> None:
    builder = PersistenceRecordBuilder()
    event = OutboxEvent(
        trace_id="trace-1",
        event_type=DomainEventType.MARKET_FILTERED_OUT.value,
        idempotency_key="idempotency-key",
        event_id="event-1",
        condition_id="condition-1",
        market_slug="sample-market-a",
        payload={
            "accepted": False,
            "parse_status": "rejected",
            "parse_reason": "missing_trading_conditions",
            "parse_detail": "missing tick_size / min_order_size",
            "condition_id": "condition-1",
            "market_slug": "sample-market-a",
            "token_ids": ["yes-token", "no-token"],
            "outcomes": [
                {"token_id": "yes-token", "outcome": "YES"},
                {"token_id": "no-token", "outcome": "NO"},
            ],
            "tick_size": "0.01",
            "min_order_size": "1",
        },
    )

    records = dict(builder.route_event(event))

    assert set(records) == {"audit", "outbox"}
    assert "source_event_type" not in records["audit"]
    assert "source_payload" not in records["audit"]
    assert "outbox_payload" not in records["outbox"]
    assert records["outbox"]["payload"]["parse_reason"] == "missing_trading_conditions"


def test_market_persistence_worker_materializes_explicit_market_snapshot() -> None:
    builder = PersistenceRecordBuilder()
    event = OutboxEvent(
        trace_id="trace-1",
        event_type=DomainEventType.MARKET_DISCOVERED.value,
        idempotency_key="idempotency-key",
        event_id="event-1",
        condition_id="condition-1",
        market_slug="sample-market-a",
        payload={
            "accepted": True,
            "parse_status": "accepted",
            "market": {
                "condition_id": "condition-1",
                "market_slug": "sample-market-a",
                "token_ids": ["yes-token", "no-token"],
                "outcomes": [
                    {"token_id": "yes-token", "outcome": "YES"},
                    {"token_id": "no-token", "outcome": "NO"},
                ],
                "tick_size": "0.01",
                "min_order_size": "1",
            },
        },
    )

    records = dict(builder.route_event(event))
    record = records["market"]

    assert record["parse_status"] == "accepted"
    assert "market_data" not in record
    assert record["condition_id"] == "condition-1"


def test_market_persistence_worker_materializes_event_slug_for_audit_and_outbox() -> None:
    builder = PersistenceRecordBuilder()
    event = OutboxEvent(
        trace_id="trace-1",
        event_type=DomainEventType.MARKET_DISCOVERED.value,
        idempotency_key="idempotency-key",
        event_id="event-1",
        condition_id="condition-1",
        market_slug="sample-market-a",
        event_slug="sample-event-a",
        payload={
            "accepted": True,
            "parse_status": "accepted",
            "market": {
                "condition_id": "condition-1",
                "market_slug": "sample-market-a",
                "event_slug": "sample-event-a",
                "token_ids": ["yes-token", "no-token"],
                "outcomes": [
                    {"token_id": "yes-token", "outcome": "YES"},
                    {"token_id": "no-token", "outcome": "NO"},
                ],
                "tick_size": "0.01",
                "min_order_size": "1",
            },
        },
    )

    records = dict(builder.route_event(event))

    assert records["audit"]["event_slug"] == "sample-event-a"
    assert records["outbox"]["event_slug"] == "sample-event-a"
    assert records["market"]["event_slug"] == "sample-event-a"


def test_market_from_record_uses_explicit_reject_reason_only() -> None:
    market = market_from_record(
        {
            "condition_id": "condition-1",
            "market_slug": "sample-market-a",
            "token_ids": ["yes-token", "no-token"],
            "outcomes": [
                {"token_id": "yes-token", "outcome": "YES"},
                {"token_id": "no-token", "outcome": "NO"},
            ],
            "tick_size": "0.01",
            "min_order_size": "1",
            "reject_reason": "missing_trading_conditions",
            "parse_reason": "missing_trading_conditions",
            "raw_payload": {
                "conditionId": "condition-1",
                "slug": "sample-market-a",
                "clobTokenIds": ["yes-token", "no-token"],
                "orderPriceMinTickSize": "0.01",
                "orderMinSize": "1",
            },
        }
    )

    assert market is not None
    assert market.reject_reason == "missing_trading_conditions"


def test_market_from_record_does_not_promote_parse_reason_to_reject_reason() -> None:
    market = market_from_record(
        {
            "condition_id": "condition-1",
            "market_slug": "sample-market-a",
            "token_ids": ["yes-token", "no-token"],
            "outcomes": [
                {"token_id": "yes-token", "outcome": "YES"},
                {"token_id": "no-token", "outcome": "NO"},
            ],
            "tick_size": "0.01",
            "min_order_size": "1",
            "parse_reason": "missing_trading_conditions",
        }
    )

    assert market is not None
    assert market.reject_reason is None


def test_market_from_record_does_not_rehydrate_legacy_market_data() -> None:
    market = market_from_record(
        {
            "condition_id": "condition-1",
            "market_slug": "sample-market-a",
            "market_data": {
                "token_ids": ["yes-token", "no-token"],
                "outcomes": [
                    {"token_id": "yes-token", "outcome": "YES"},
                    {"token_id": "no-token", "outcome": "NO"},
                ],
                "tick_size": "0.01",
                "min_order_size": "1",
            },
        }
    )

    assert market is None


def test_market_from_record_does_not_promote_source_event_id() -> None:
    market = market_from_record(
        {
            "condition_id": "condition-1",
            "market_slug": "sample-market-a",
            "token_ids": ["yes-token", "no-token"],
            "outcomes": [
                {"token_id": "yes-token", "outcome": "YES"},
                {"token_id": "no-token", "outcome": "NO"},
            ],
            "tick_size": "0.01",
            "min_order_size": "1",
            "source_event_id": "legacy-event",
        }
    )

    assert market is not None
    assert market.event_id is None


def test_outbox_event_from_record_uses_explicit_payload_only() -> None:
    event = outbox_event_from_record(
        {
            "trace_id": "trace-1",
            "event_type": "market_discovered",
            "idempotency_key": "idem-1",
            "outbox_payload": {"legacy": True},
            "raw_payload": {"fallback": True},
        }
    )

    assert event is not None
    assert event.payload == {}


def test_fill_from_record_requires_event_type_and_explicit_size() -> None:
    assert fill_from_record({"trace_id": "trace-1", "event_id": "event-1"}) is None

    fill = fill_from_record(
        {
            "trace_id": "trace-1",
            "event_id": "event-1",
            "event_type": "trade_confirmed",
            "filled_shares": "2",
        }
    )

    assert fill is not None
    assert fill.size is None
