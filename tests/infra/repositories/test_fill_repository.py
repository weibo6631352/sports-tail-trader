"""``FillRepository`` round-trip / 查询覆盖。

PG 用例验证 fill 字段映射、``event_id`` 幂等 upsert、
trace/condition/order/time-range 过滤。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from polymarket_trader.domain.events import DomainEventType, Fill
from polymarket_trader.domain.time_filters import TimeRange


def _make_fill(
    *,
    event_id: str = "fill-evt-1",
    trace_id: str = "trace-1",
    condition_id: str = "cond-1",
    token_id: str = "tok-1",
    order_id: str = "order-1",
    trade_id: str = "trade-1",
    side: str = "BUY",
    price: Decimal = Decimal("0.50"),
    size: Decimal = Decimal("100"),
    confirmed_at: datetime | None = None,
    strategy_id: str = "sports_tail",
) -> Fill:
    confirmed_at = confirmed_at or datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    return Fill(
        strategy_id=strategy_id,
        trace_id=trace_id,
        event_type=DomainEventType.TRADE_CONFIRMED,
        event_id=event_id,
        market_slug=f"slug-{condition_id}",
        condition_id=condition_id,
        token_id=token_id,
        order_id=order_id,
        trade_id=trade_id,
        side=side,
        price=price,
        size=size,
        notional_usdc=price * size,
        status="confirmed",
        confirmed_at=confirmed_at,
    )


@pytest.mark.pg
async def test_fill_repository_save_and_round_trip(pg_session_factory: Any) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import FillRepository
    from polymarket_trader.infra.db.models import FillModel

    fill = _make_fill()

    async with pg_session_factory() as session:
        await session.execute(delete(FillModel))
        repo = FillRepository(session)
        await repo.save_fill(fill)
        await session.commit()

    async with pg_session_factory() as session:
        repo = FillRepository(session)
        page = await repo.list_fills_snapshot(trace_id="trace-1")

    assert page.total == 1
    restored = page.items[0]
    assert restored.event_id == fill.event_id
    assert restored.order_id == fill.order_id
    assert restored.trade_id == fill.trade_id
    assert restored.side == "BUY"
    assert restored.price == Decimal("0.50")
    assert restored.size == Decimal("100")
    assert restored.notional_usdc == Decimal("50.00")
    assert restored.status == "confirmed"


@pytest.mark.pg
async def test_fill_repository_upsert_dedupes_on_event_id(pg_session_factory: Any) -> None:
    from sqlalchemy import delete, func, select

    from polymarket_trader.infra.db import FillRepository
    from polymarket_trader.infra.db.models import FillModel

    first = _make_fill(event_id="dup-evt", price=Decimal("0.40"))
    second = _make_fill(event_id="dup-evt", price=Decimal("0.55"))

    async with pg_session_factory() as session:
        await session.execute(delete(FillModel))
        repo = FillRepository(session)
        await repo.save_fills([first, second])
        await session.commit()

    async with pg_session_factory() as session:
        total = await session.scalar(select(func.count(FillModel.id)))
        repo = FillRepository(session)
        page = await repo.list_fills_snapshot()

    assert total == 1
    assert page.items[0].price == Decimal("0.55")


@pytest.mark.pg
async def test_fill_repository_filters_by_order_condition_and_time_window(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import FillRepository
    from polymarket_trader.infra.db.models import FillModel

    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    fills = [
        _make_fill(event_id="f-old", order_id="order-A", confirmed_at=base - timedelta(hours=2)),
        _make_fill(event_id="f-mid", order_id="order-A", confirmed_at=base - timedelta(minutes=10)),
        _make_fill(
            event_id="f-other",
            order_id="order-B",
            condition_id="cond-B",
            confirmed_at=base,
        ),
    ]

    async with pg_session_factory() as session:
        await session.execute(delete(FillModel))
        repo = FillRepository(session)
        await repo.save_fills(fills)
        await session.commit()

    async with pg_session_factory() as session:
        repo = FillRepository(session)
        by_order = await repo.list_fills_snapshot(order_id="order-A")
        by_condition = await repo.list_fills_snapshot(condition_id="cond-B")
        since_ms = int((base - timedelta(hours=1)).timestamp() * 1000)
        windowed = await repo.list_fills_snapshot(time_range=TimeRange(since_ms=since_ms))

    assert by_order.total == 2
    assert {f.event_id for f in by_order.items} == {"f-old", "f-mid"}
    assert by_condition.total == 1 and by_condition.items[0].event_id == "f-other"
    assert {f.event_id for f in windowed.items} == {"f-mid", "f-other"}
