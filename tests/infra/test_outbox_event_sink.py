from __future__ import annotations

from typing import Any

import pytest

from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.infra.outbox.event_sink import build_domain_event_outbox_sink


class _CollectingOutbox:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def put_nowait(self, event: Any) -> bool:
        self.events.append(event)
        return True


@pytest.mark.parametrize(
    "event_type",
    (
        DomainEventType.MARKET_DISCOVERED,
        DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED,
        DomainEventType.SKIPPED,
    ),
)
def test_non_transaction_runtime_events_are_not_persisted(event_type: DomainEventType) -> None:
    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)
    event = DomainEvent(
        trace_id=f"trace-{event_type.value}",
        event_type=event_type,
        event_id="event-1",
        market_slug="nba-game-moneyline",
        condition_id="condition-1",
        payload={
            "source": "gamma.events_keyset",
            "summary": {"market_slug": "nba-game-moneyline"},
            "parse_status": "accepted",
            "accepted": True,
            "discovery_kind": "market_discovered",
            "raw_market": {
                "condition_id": "condition-1",
                "private_key": "should-not-enter-outbox",
                "nested": {"token": "should-not-enter-outbox"},
                "large_blob": "x" * 20_000,
            },
            "market": {
                "condition_id": "condition-1",
                "market_slug": "nba-game-moneyline",
                "token_ids": ["token-yes", "token-no"],
                "outcomes": [
                    {"token_id": "token-yes", "outcome": "Yes"},
                    {"token_id": "token-no", "outcome": "No"},
                ],
                "fees": {"enabled": True, "fee_rate_bps": 0},
            },
        },
    )

    sink(3, event)

    assert outbox.events == []


def test_transaction_snapshot_event_is_persisted() -> None:
    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)
    event = DomainEvent(
        trace_id="trace-order",
        event_type=DomainEventType.ORDER_STATE_UPDATED,
        event_id="event-order-1",
        market_slug="nba-game-moneyline",
        condition_id="condition-1",
        token_id="token-yes",
        payload={
            "order": {
                "order_id": "order-1",
                "condition_id": "condition-1",
                "token_id": "token-yes",
                "status": "live",
            },
            "snapshot": {"large_runtime_state": "not-needed"},
        },
    )

    sink(0, event)

    assert len(outbox.events) == 1
    assert outbox.events[0].event_type == DomainEventType.ORDER_STATE_UPDATED.value
    assert outbox.events[0].payload == {
        "order": {
            "order_id": "order-1",
            "condition_id": "condition-1",
            "token_id": "token-yes",
            "status": "live",
        }
    }
