from __future__ import annotations

import asyncio
from datetime import datetime

from polymarket_trader.domain.events import OutboxEvent, sanitize_raw_response
from polymarket_trader.infra.outbox.local_queue import LocalOutbox


def test_local_outbox_merges_retained_low_priority_events() -> None:
    async def run() -> None:
        outbox = LocalOutbox(max_size=1, retained_max_size=2)
        first = OutboxEvent(
            trace_id="trace",
            event_type="market_discovered",
            idempotency_key="idem-1",
            market_slug="market",
            priority="P2",
            created_at=datetime(2026, 1, 1, 12, 0, 0),
            raw_response_summary={"authorization": "Bearer abc123"},
        )
        second = OutboxEvent(
            trace_id="trace",
            event_type="market_discovered",
            idempotency_key="idem-2",
            market_slug="market",
            priority="P2",
            created_at=datetime(2026, 1, 1, 12, 0, 1),
            raw_response_summary={"authorization": "Bearer updated"},
        )

        assert await outbox.enqueue(first)
        assert await outbox.enqueue(second)

        queued = await outbox.get()
        assert queued.event_id == first.event_id
        assert queued.raw_response_summary == sanitize_raw_response(
            {"authorization": "Bearer updated"},
            max_length=512,
        )

        await outbox.ack(queued)
        assert outbox.get_dead_letters() == ()

    asyncio.run(run())


def test_local_outbox_put_nowait_and_retry_round_trip() -> None:
    async def run() -> None:
        outbox = LocalOutbox(max_size=2)
        event = OutboxEvent(
            trace_id="trace",
            event_type="order_created",
            idempotency_key="idem-3",
            market_slug="market",
            priority="P0",
            raw_response_summary={"token": "super-secret"},
        )

        assert outbox.put_nowait(event)
        retried = await outbox.retry(event, last_error="transient")
        assert retried.retry_count == 1
        assert retried.last_error == "transient"

        queued = await outbox.get()
        assert queued.event_id == event.event_id
        assert queued.raw_response_summary is not None
        await outbox.ack(queued)

    asyncio.run(run())
