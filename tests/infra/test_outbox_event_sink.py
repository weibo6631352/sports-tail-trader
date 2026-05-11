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
        DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED,
        DomainEventType.SKIPPED,
    ),
)
def test_non_persistable_event_types_are_dropped(event_type: DomainEventType) -> None:
    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)
    event = DomainEvent(
        trace_id=f"trace-{event_type.value}",
        event_type=event_type,
        event_id="event-1",
        market_slug="nba-game-moneyline",
        condition_id="condition-1",
        payload={"unused": "should-not-enter-outbox"},
    )

    sink(3, event)

    assert outbox.events == []


def test_market_discovered_event_is_persisted_with_stripped_payload() -> None:
    """discovery 事件需要落库（CLAUDE.md §10 可审计），但 raw_market 这类
    重型字段必须在 _project_payload 阶段剔除，避免 audit 表暴涨。"""

    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)
    event = DomainEvent(
        trace_id="trace-market-discovered",
        event_type=DomainEventType.MARKET_DISCOVERED,
        event_id="event-md-1",
        market_slug="nba-game-moneyline",
        condition_id="condition-1",
        reason="market_selected",
        payload={
            "source": "gamma.events_keyset",
            "summary": "nba-game-moneyline",
            "parse_status": "accepted",
            "accepted": True,
            "discovery_kind": "market_discovered",
            "extension_reason": "market_selected",
            "raw_market": {
                "condition_id": "condition-1",
                "large_blob": "x" * 20_000,
            },
            "market": {
                "condition_id": "condition-1",
                "market_slug": "nba-game-moneyline",
                "token_ids": ["token-yes", "token-no"],
            },
        },
    )

    sink(3, event)

    assert len(outbox.events) == 1
    persisted = outbox.events[0]
    assert persisted.event_type == "market_discovered"
    assert persisted.reason == "market_selected"
    assert persisted.payload.get("market", {}).get("market_slug") == "nba-game-moneyline"
    assert "raw_market" not in persisted.payload
    assert persisted.payload.get("accepted") is True
    assert persisted.payload.get("discovery_kind") == "market_discovered"


def test_market_filtered_out_event_is_persisted_for_audit() -> None:
    """universe 拒绝首次目标盘口时必须留下可审计记录。"""

    outbox = _CollectingOutbox()
    sink = build_domain_event_outbox_sink(outbox)
    event = DomainEvent(
        trace_id="trace-filtered",
        event_type=DomainEventType.MARKET_FILTERED_OUT,
        event_id="event-mfo-1",
        market_slug="nba-futures-champion",
        condition_id="condition-2",
        reason="market_family_not_single_game",
        payload={
            "source": "gamma.events_keyset",
            "accepted": False,
            "discovery_kind": "market_filtered_out",
            "extension_reason": "market_family_not_single_game",
            "raw_market": {"large_blob": "x" * 20_000},
        },
    )

    sink(3, event)

    assert len(outbox.events) == 1
    persisted = outbox.events[0]
    assert persisted.event_type == "market_filtered_out"
    assert persisted.reason == "market_family_not_single_game"
    assert persisted.payload.get("extension_reason") == "market_family_not_single_game"
    assert "raw_market" not in persisted.payload


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
