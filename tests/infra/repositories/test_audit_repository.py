"""``AuditEventRepository`` round-trip / 查询覆盖。

PG 用例验证 audit 行 ``event_id`` 幂等 upsert、trace/event_title/condition
过滤、时间窗筛选。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from polymarket_trader.domain.events import AuditEvent
from polymarket_trader.domain.time_filters import TimeRange


def _make_audit_event(
    *,
    event_id: str = "audit-1",
    trace_id: str = "trace-1",
    event_title: str = "order_submitted",
    condition_id: str | None = "cond-1",
    token_id: str | None = "tok-1",
    strategy_id: str = "sports_tail",
    created_at: datetime | None = None,
    price: Decimal | None = Decimal("0.50"),
    size: Decimal | None = Decimal("100"),
) -> AuditEvent:
    return AuditEvent(
        event_title=event_title,
        trace_id=trace_id,
        strategy_id=strategy_id,
        event_id=event_id,
        market_slug=f"slug-{condition_id}" if condition_id else None,
        condition_id=condition_id,
        token_id=token_id,
        side="BUY",
        order_type="GTC",
        price=price,
        size=size,
        notional_usdc=None if price is None or size is None else price * size,
        order_id="order-1",
        status="submitted",
        reason="signal_triggered",
        created_at=created_at,
    )


@pytest.mark.pg
async def test_audit_repository_save_and_round_trip(pg_session_factory: Any) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import AuditEventRepository
    from polymarket_trader.infra.db.models import AuditEventModel

    event = _make_audit_event()

    async with pg_session_factory() as session:
        await session.execute(delete(AuditEventModel))
        repo = AuditEventRepository(session)
        await repo.save_audit_event(event)
        await session.commit()

    async with pg_session_factory() as session:
        repo = AuditEventRepository(session)
        page = await repo.list_audit_events_snapshot(trace_id="trace-1")

    assert page.total == 1
    restored = page.items[0]
    assert restored.event_id == "audit-1"
    assert restored.event_title == "order_submitted"
    assert restored.trace_id == "trace-1"
    assert restored.strategy_id == "sports_tail"


@pytest.mark.pg
async def test_audit_repository_upsert_dedupes_on_event_id(pg_session_factory: Any) -> None:
    from sqlalchemy import delete, func, select

    from polymarket_trader.infra.db import AuditEventRepository
    from polymarket_trader.infra.db.models import AuditEventModel

    first = _make_audit_event(event_id="dup", event_title="order_submitted")
    second = _make_audit_event(event_id="dup", event_title="order_matched")

    async with pg_session_factory() as session:
        await session.execute(delete(AuditEventModel))
        repo = AuditEventRepository(session)
        await repo.save_audit_events([first, second])
        await session.commit()

    async with pg_session_factory() as session:
        total = await session.scalar(select(func.count(AuditEventModel.id)))
        repo = AuditEventRepository(session)
        page = await repo.list_audit_events_snapshot()

    assert total == 1
    assert page.items[0].event_title == "order_matched"


@pytest.mark.pg
async def test_audit_repository_filters_by_event_title_condition_and_window(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import AuditEventRepository
    from polymarket_trader.infra.db.models import AuditEventModel

    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        _make_audit_event(
            event_id="e-old",
            event_title="order_submitted",
            condition_id="cond-A",
            created_at=base - timedelta(hours=2),
        ),
        _make_audit_event(
            event_id="e-mid",
            event_title="order_matched",
            condition_id="cond-A",
            created_at=base - timedelta(minutes=15),
        ),
        _make_audit_event(
            event_id="e-other",
            event_title="order_submitted",
            condition_id="cond-B",
            created_at=base,
        ),
    ]

    async with pg_session_factory() as session:
        await session.execute(delete(AuditEventModel))
        repo = AuditEventRepository(session)
        await repo.save_audit_events(events)
        await session.commit()

    async with pg_session_factory() as session:
        repo = AuditEventRepository(session)
        submitted = await repo.list_audit_events_snapshot(event_title="order_submitted")
        cond_a = await repo.list_audit_events_snapshot(condition_id="cond-A")
        since_ms = int((base - timedelta(hours=1)).timestamp() * 1000)
        windowed = await repo.list_audit_events_snapshot(time_range=TimeRange(since_ms=since_ms))

    assert {e.event_id for e in submitted.items} == {"e-old", "e-other"}
    assert {e.event_id for e in cond_a.items} == {"e-old", "e-mid"}
    assert {e.event_id for e in windowed.items} == {"e-mid", "e-other"}
