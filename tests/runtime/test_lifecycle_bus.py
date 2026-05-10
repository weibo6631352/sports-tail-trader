from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from polymarket_trader.extension_api.lifecycle import LifecycleEnvelope, LifecycleEvent
from polymarket_trader.runtime.lifecycle_bus import InProcessLifecycleBus


def _envelope(event: LifecycleEvent = LifecycleEvent.ORDER_FILLED) -> LifecycleEnvelope:
    return LifecycleEnvelope(
        event=event,
        occurred_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        trace_id="trace-1",
        condition_id="cond-1",
        token_id="tok-1",
        market_slug="slug-1",
        payload={"key": "value"},
    )


def test_lifecycle_bus_sync_publish_invokes_subscriber() -> None:
    bus = InProcessLifecycleBus()
    received: list[LifecycleEnvelope] = []

    async def callback(envelope: LifecycleEnvelope) -> None:
        received.append(envelope)

    bus.subscribe(LifecycleEvent.ORDER_FILLED, callback)
    bus.publish(LifecycleEvent.ORDER_FILLED, trace_id="trace-1", condition_id="cond-1")

    assert len(received) == 1
    assert received[0].event == LifecycleEvent.ORDER_FILLED
    assert received[0].trace_id == "trace-1"


def test_lifecycle_bus_async_publish_invokes_subscriber() -> None:
    bus = InProcessLifecycleBus()
    received: list[LifecycleEnvelope] = []

    async def callback(envelope: LifecycleEnvelope) -> None:
        received.append(envelope)

    async def driver() -> None:
        bus.subscribe(LifecycleEvent.ORDER_FILLED, callback)
        bus.publish(LifecycleEvent.ORDER_FILLED, trace_id="trace-async")
        # Give scheduled callback time to run.
        await asyncio.sleep(0)

    asyncio.run(driver())
    assert len(received) == 1
    assert received[0].trace_id == "trace-async"


def test_lifecycle_bus_unsubscribe_stops_callbacks() -> None:
    bus = InProcessLifecycleBus()
    received: list[LifecycleEnvelope] = []

    async def callback(envelope: LifecycleEnvelope) -> None:
        received.append(envelope)

    handle = bus.subscribe(LifecycleEvent.ORDER_FILLED, callback)
    bus.unsubscribe(handle)
    bus.publish(LifecycleEvent.ORDER_FILLED)

    assert received == []


def test_lifecycle_bus_callback_exception_does_not_block_other_subscribers() -> None:
    bus = InProcessLifecycleBus()
    received: list[str] = []

    async def crashing(envelope: LifecycleEnvelope) -> None:
        raise RuntimeError("boom")

    async def healthy(envelope: LifecycleEnvelope) -> None:
        received.append("ok")

    bus.subscribe(LifecycleEvent.ORDER_REJECTED, crashing)
    bus.subscribe(LifecycleEvent.ORDER_REJECTED, healthy)
    bus.publish(LifecycleEvent.ORDER_REJECTED)

    assert received == ["ok"]


def test_lifecycle_bus_only_dispatches_subscribed_event() -> None:
    bus = InProcessLifecycleBus()
    received: list[LifecycleEnvelope] = []

    async def callback(envelope: LifecycleEnvelope) -> None:
        received.append(envelope)

    bus.subscribe(LifecycleEvent.ORDER_FILLED, callback)
    bus.publish(LifecycleEvent.ORDER_REJECTED)

    assert received == []
