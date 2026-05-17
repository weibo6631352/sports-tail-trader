from __future__ import annotations

import asyncio
from types import SimpleNamespace

from polymarket_trader.domain.events import OutboxPriority
from polymarket_trader.runtime.event_bus import EventBus


def _event(
    event_id: str,
    *,
    event_type: str = "orderbook_snapshot_updated",
    merge_key: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        trace_id=f"trace-{event_id}",
        event_id=event_id,
        event_type=event_type,
        merge_key=merge_key,
    )


def test_trading_lane_keeps_latest_unconsumed_event_per_merge_key() -> None:
    async def run() -> None:
        bus = EventBus(trading_capacity=10)
        await bus.publish(OutboxPriority.P1, _event("old", merge_key="orderbook|token-1"))
        await bus.publish(OutboxPriority.P1, _event("new", merge_key="orderbook|token-1"))

        event = await asyncio.wait_for(bus.next_trading_event(), timeout=0.1)

        assert event.event_id == "new"
        assert bus.trading_queue_depth() == 0

    asyncio.run(run())


def test_generic_consumer_reads_coalesced_trading_event() -> None:
    async def run() -> None:
        bus = EventBus(trading_capacity=10)
        await bus.publish(OutboxPriority.P1, _event("old", merge_key="orderbook|token-1"))
        await bus.publish(OutboxPriority.P1, _event("new", merge_key="orderbook|token-1"))

        event = await asyncio.wait_for(bus.next_event(), timeout=0.1)

        assert event.event_id == "new"
        assert bus.trading_queue_depth() == 0

    asyncio.run(run())


def test_trading_lane_does_not_coalesce_events_without_merge_key() -> None:
    async def run() -> None:
        bus = EventBus(trading_capacity=10)
        await bus.publish(OutboxPriority.P1, _event("first"))
        await bus.publish(OutboxPriority.P1, _event("second"))

        first = await asyncio.wait_for(bus.next_trading_event(), timeout=0.1)
        second = await asyncio.wait_for(bus.next_trading_event(), timeout=0.1)

        assert first.event_id == "first"
        assert second.event_id == "second"

    asyncio.run(run())


def test_trading_lane_coalesces_any_event_with_merge_key() -> None:
    async def run() -> None:
        bus = EventBus(trading_capacity=10)
        await bus.publish(
            OutboxPriority.P0,
            _event(
                "first",
                event_type="entry_signal_triggered",
                merge_key="orderbook_snapshot_updated|token-1",
            ),
        )
        await bus.publish(
            OutboxPriority.P0,
            _event(
                "second",
                event_type="entry_signal_triggered",
                merge_key="orderbook_snapshot_updated|token-1",
            ),
        )

        event = await asyncio.wait_for(bus.next_trading_event(), timeout=0.1)
        assert event.event_id == "second"
        assert bus.trading_queue_depth() == 0

    asyncio.run(run())
