"""``OutboxEventRepository`` round-trip / 查询覆盖。

PG 用例验证 outbox 事件按 ``idempotency_key`` upsert、按事件类型白名单
回放、失败/重试视图、时间窗与 trace 过滤。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from polymarket_trader.domain.events import OutboxEvent
from polymarket_trader.domain.time_filters import TimeRange


def _make_event(
    *,
    idempotency_key: str = "ev-key-1",
    event_id: str | None = None,
    trace_id: str = "trace-1",
    event_type: str = "order_submitted",
    condition_id: str | None = "cond-1",
    token_id: str | None = "tok-1",
    priority: int = 0,
    retry_count: int = 0,
    last_error: str | None = None,
    created_at: datetime | None = None,
) -> OutboxEvent:
    kwargs: dict[str, Any] = {
        "trace_id": trace_id,
        "event_type": event_type,
        "idempotency_key": idempotency_key,
        "market_slug": f"slug-{condition_id}" if condition_id else None,
        "condition_id": condition_id,
        "token_id": token_id,
        "reason": "signal",
        "priority": priority,
        "retry_count": retry_count,
        "last_error": last_error,
        "payload": {"order_id": "ord-1"},
    }
    if event_id is not None:
        kwargs["event_id"] = event_id
    if created_at is not None:
        kwargs["created_at"] = created_at
    return OutboxEvent(**kwargs)


@pytest.mark.pg
async def test_outbox_repository_save_and_list_pending(pg_session_factory: Any) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import OutboxEventRepository
    from polymarket_trader.infra.db.models import OutboxEventModel

    event = _make_event()

    async with pg_session_factory() as session:
        await session.execute(delete(OutboxEventModel))
        repo = OutboxEventRepository(session)
        await repo.save_event(event)
        await session.commit()

    async with pg_session_factory() as session:
        repo = OutboxEventRepository(session)
        page = await repo.list_pending_snapshot(trace_id="trace-1")

    assert page.total == 1
    restored = page.items[0]
    assert restored.idempotency_key == "ev-key-1"
    assert restored.event_type == "order_submitted"
    assert restored.condition_id == "cond-1"
    assert restored.priority == 0
    assert dict(restored.payload) == {"order_id": "ord-1"}


@pytest.mark.pg
async def test_outbox_repository_upsert_dedupes_on_idempotency_key(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete, func, select

    from polymarket_trader.infra.db import OutboxEventRepository
    from polymarket_trader.infra.db.models import OutboxEventModel

    first = _make_event(idempotency_key="dup-key", retry_count=0, last_error=None)
    second = _make_event(idempotency_key="dup-key", retry_count=2, last_error="boom")

    async with pg_session_factory() as session:
        await session.execute(delete(OutboxEventModel))
        repo = OutboxEventRepository(session)
        await repo.save_events([first, second])
        await session.commit()

    async with pg_session_factory() as session:
        total = await session.scalar(select(func.count(OutboxEventModel.id)))
        repo = OutboxEventRepository(session)
        page = await repo.list_pending_snapshot()

    assert total == 1
    assert page.items[0].retry_count == 2
    assert page.items[0].last_error == "boom"


@pytest.mark.pg
async def test_outbox_repository_lists_by_event_types_with_window(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import OutboxEventRepository
    from polymarket_trader.infra.db.models import OutboxEventModel

    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        _make_event(
            idempotency_key="key-old",
            event_type="order_submitted",
            created_at=base - timedelta(hours=3),
        ),
        _make_event(
            idempotency_key="key-mid",
            event_type="order_matched",
            created_at=base - timedelta(minutes=30),
        ),
        _make_event(
            idempotency_key="key-new",
            event_type="fill_recorded",
            created_at=base,
        ),
    ]

    async with pg_session_factory() as session:
        await session.execute(delete(OutboxEventModel))
        repo = OutboxEventRepository(session)
        await repo.save_events(events)
        await session.commit()

    async with pg_session_factory() as session:
        repo = OutboxEventRepository(session)
        by_type = await repo.list_events_by_types_snapshot(
            event_types=("order_submitted", "fill_recorded"),
        )
        since_ms = int((base - timedelta(hours=1)).timestamp() * 1000)
        windowed = await repo.list_events_by_types_snapshot(
            event_types=("order_submitted", "order_matched", "fill_recorded"),
            time_range=TimeRange(since_ms=since_ms),
        )
        empty = await repo.list_events_by_types_snapshot(event_types=())

    assert {e.idempotency_key for e in by_type.items} == {"key-old", "key-new"}
    assert {e.idempotency_key for e in windowed.items} == {"key-mid", "key-new"}
    assert empty.total == 0


@pytest.mark.pg
async def test_outbox_repository_list_failures_filters_by_retry_and_last_error(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import OutboxEventRepository
    from polymarket_trader.infra.db.models import OutboxEventModel

    healthy = _make_event(idempotency_key="ok", retry_count=0, last_error=None)
    retried = _make_event(idempotency_key="retried", retry_count=2, last_error=None)
    errored = _make_event(idempotency_key="errored", retry_count=0, last_error="db_timeout")

    async with pg_session_factory() as session:
        await session.execute(delete(OutboxEventModel))
        repo = OutboxEventRepository(session)
        await repo.save_events([healthy, retried, errored])
        await session.commit()

    async with pg_session_factory() as session:
        repo = OutboxEventRepository(session)
        failures = await repo.list_failures_snapshot(min_retry_count=1)

    assert {e.idempotency_key for e in failures.items} == {"retried", "errored"}
