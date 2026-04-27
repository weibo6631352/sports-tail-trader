from __future__ import annotations

import asyncio

from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.infra.outbox import LocalOutbox, build_domain_event_outbox_sink
from polymarket_trader.runtime.event_bus import EventBus


def _event(
    *,
    trace_id: str,
    event_id: str,
    event_type: DomainEventType,
    reason: str,
) -> DomainEvent:
    return DomainEvent(
        trace_id=trace_id,
        event_type=event_type,
        event_id=event_id,
        reason=reason,
    )


def test_event_bus_prioritizes_trading_and_restores_retained_low_priority_events() -> None:
    async def run() -> None:
        bus = EventBus(
            trading_capacity=2,
            maintenance_capacity=1,
            persistence_capacity=1,
            retained_capacity=4,
        )
        bus.pause_low_priority()

        maintenance_event = _event(
            trace_id="trace-maintenance",
            event_id="event-maintenance",
            event_type=DomainEventType.MARKET_UPDATED,
            reason="maintenance",
        )
        persistence_event = _event(
            trace_id="trace-persistence",
            event_id="event-persistence",
            event_type=DomainEventType.RETRY,
            reason="persistence",
        )
        trading_event = _event(
            trace_id="trace-trading",
            event_id="event-trading",
            event_type=DomainEventType.RISK_CHECK_PASSED,
            reason="trading",
        )

        await bus.publish(OutboxPriority.P2, maintenance_event)
        await bus.publish(OutboxPriority.P3, persistence_event)
        await bus.publish(OutboxPriority.P0, trading_event)

        paused_snapshot = bus.snapshot()
        assert paused_snapshot.low_priority_paused is True
        assert paused_snapshot.trading_queue_depth == 1
        assert paused_snapshot.maintenance_retained_depth == 1
        assert paused_snapshot.persistence_retained_depth == 1

        assert await bus.next_event() == trading_event

        await bus.resume_low_priority()
        resumed_snapshot = bus.snapshot()
        assert resumed_snapshot.low_priority_paused is False
        assert resumed_snapshot.maintenance_retained_depth == 0
        assert resumed_snapshot.persistence_retained_depth == 0

        assert await bus.next_event() == maintenance_event
        assert await bus.next_event() == persistence_event

        final_snapshot = bus.snapshot()
        assert final_snapshot.trading_queue_depth == 0
        assert final_snapshot.maintenance_queue_depth == 0
        assert final_snapshot.persistence_queue_depth == 0

    asyncio.run(run())


def test_event_bus_mirrors_supported_domain_events_and_trims_user_payloads() -> None:
    async def run() -> None:
        bus = EventBus()
        outbox = LocalOutbox(max_size=8)
        bus.bind_persistence_sink(build_domain_event_outbox_sink(outbox))

        market_event = _event(
            trace_id="trace-market",
            event_id="event-market",
            event_type=DomainEventType.MARKET_DISCOVERED,
            reason="market_discovered",
        )
        order_event = DomainEvent(
            trace_id="trace-order",
            event_id="event-order",
            event_type=DomainEventType.ORDER_STATE_UPDATED,
            condition_id="condition",
            token_id="token",
            market_slug="sample-market-a",
            reason="order_update",
            payload={
                "order": {
                    "condition_id": "condition",
                    "token_id": "token",
                    "side": "BUY",
                    "order_type": "FAK",
                    "price": "0.43",
                },
                "open_orders": [{"order_id": "order-1"}],
                "snapshot": {"open_orders": [{"order_id": "order-1"}]},
            },
        )
        non_domain_event = _event(
            trace_id="trace-risk",
            event_id="event-risk",
            event_type=DomainEventType.RISK_CHECK_PASSED,
            reason="risk_ok",
        )

        await bus.publish(OutboxPriority.P2, market_event)
        await bus.publish(OutboxPriority.P0, order_event)
        await bus.publish(OutboxPriority.P0, non_domain_event)

        order_queued = await outbox.get()
        market_queued = await outbox.get()

        assert order_queued.event_id == "event-order"
        assert order_queued.event_type == DomainEventType.ORDER_STATE_UPDATED.value
        assert order_queued.priority == 0
        assert "order" in order_queued.payload
        assert "open_orders" not in order_queued.payload
        assert "snapshot" not in order_queued.payload

        assert market_queued.event_id == "event-market"
        assert market_queued.event_type == DomainEventType.MARKET_DISCOVERED.value
        assert market_queued.priority == 2

        try:
            await asyncio.wait_for(outbox.get(), timeout=0.05)
        except asyncio.TimeoutError:
            pass
        else:  # pragma: no cover - defensive assertion path
            raise AssertionError("unsupported events should not be mirrored into the outbox")

    asyncio.run(run())


def test_event_bus_does_not_mirror_routine_market_updated_events() -> None:
    async def run() -> None:
        bus = EventBus()
        outbox = LocalOutbox(max_size=8)
        bus.bind_persistence_sink(build_domain_event_outbox_sink(outbox))

        await bus.publish(
            OutboxPriority.P2,
            _event(
                trace_id="trace-market",
                event_id="event-market",
                event_type=DomainEventType.MARKET_UPDATED,
                reason="market_snapshot",
            ),
        )

        try:
            await asyncio.wait_for(outbox.get(), timeout=0.05)
        except asyncio.TimeoutError:
            pass
        else:  # pragma: no cover - defensive assertion path
            raise AssertionError("routine market updates should not be mirrored into the outbox")

    asyncio.run(run())


def test_event_bus_bypasses_persistence_lane_when_outbox_sink_is_bound() -> None:
    async def run() -> None:
        bus = EventBus()
        outbox = LocalOutbox(max_size=8)
        bus.bind_persistence_sink(build_domain_event_outbox_sink(outbox))

        event = _event(
            trace_id="trace-persistence-only",
            event_id="event-persistence-only",
            event_type=DomainEventType.MARKET_FILTERED_OUT,
            reason="filtered_out",
        )

        await bus.publish(OutboxPriority.P3, event)

        queued = await outbox.get()
        assert queued.event_id == "event-persistence-only"
        assert queued.priority == 3
        assert bus.snapshot().persistence_queue_depth == 0

    asyncio.run(run())
