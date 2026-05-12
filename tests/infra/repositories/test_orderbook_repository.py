"""``OrderbookSnapshotRepository`` round-trip / 查询覆盖。

PG 用例验证 snapshot upsert key（``snapshot_key``）、最新快照查询、
``token_id`` / ``condition_id`` / 时间窗过滤。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.time_filters import TimeRange


def _make_snapshot(
    *,
    token_id: str = "tok-1",
    condition_id: str = "cond-1",
    received_at: datetime | None = None,
    best_bid: Decimal = Decimal("0.50"),
    best_ask: Decimal = Decimal("0.52"),
) -> OrderbookSnapshot:
    received_at = received_at or datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    return OrderbookSnapshot(
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        bids=(PriceLevel(price=best_bid, size=Decimal("100")),),
        asks=(PriceLevel(price=best_ask, size=Decimal("80")),),
        received_at=received_at,
        market_slug=f"slug-{condition_id}",
        condition_id=condition_id,
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("80"),
        last_trade_price=Decimal("0.51"),
        tick_size=Decimal("0.01"),
    )


@pytest.mark.pg
async def test_orderbook_repository_save_and_round_trip(pg_session_factory: Any) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import OrderbookSnapshotRepository
    from polymarket_trader.infra.db.models import OrderbookSnapshotModel

    snapshot = _make_snapshot()

    async with pg_session_factory() as session:
        await session.execute(delete(OrderbookSnapshotModel))
        repo = OrderbookSnapshotRepository(session)
        await repo.save_snapshot(snapshot, trace_id="trace-1", source="ws")
        await session.commit()

    async with pg_session_factory() as session:
        repo = OrderbookSnapshotRepository(session)
        latest = await repo.list_latest_snapshot(snapshot.token_id)

    assert latest is not None
    assert latest.token_id == snapshot.token_id
    assert latest.condition_id == snapshot.condition_id
    assert latest.best_bid == Decimal("0.50")
    assert latest.best_ask == Decimal("0.52")
    assert latest.bids[0].size == Decimal("100")
    assert latest.asks[0].price == Decimal("0.52")
    assert latest.tick_size == Decimal("0.01")


@pytest.mark.pg
async def test_orderbook_repository_latest_snapshot_returns_most_recent(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import OrderbookSnapshotRepository
    from polymarket_trader.infra.db.models import OrderbookSnapshotModel

    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    snapshots = [
        _make_snapshot(received_at=base - timedelta(minutes=10), best_bid=Decimal("0.40")),
        _make_snapshot(received_at=base, best_bid=Decimal("0.55")),
    ]

    async with pg_session_factory() as session:
        await session.execute(delete(OrderbookSnapshotModel))
        repo = OrderbookSnapshotRepository(session)
        await repo.save_snapshots(snapshots)
        await session.commit()

    async with pg_session_factory() as session:
        repo = OrderbookSnapshotRepository(session)
        latest = await repo.list_latest_snapshot("tok-1")

    assert latest is not None
    assert latest.best_bid == Decimal("0.55")


@pytest.mark.pg
async def test_orderbook_repository_list_filters_by_condition_and_time(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import OrderbookSnapshotRepository
    from polymarket_trader.infra.db.models import OrderbookSnapshotModel

    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    seeds = [
        _make_snapshot(token_id="tok-A", condition_id="cond-A", received_at=base - timedelta(hours=2)),
        _make_snapshot(token_id="tok-A", condition_id="cond-A", received_at=base),
        _make_snapshot(token_id="tok-B", condition_id="cond-B", received_at=base),
    ]

    async with pg_session_factory() as session:
        await session.execute(delete(OrderbookSnapshotModel))
        repo = OrderbookSnapshotRepository(session)
        await repo.save_snapshots(seeds)
        await session.commit()

    async with pg_session_factory() as session:
        repo = OrderbookSnapshotRepository(session)
        cond_a = await repo.list_snapshots(condition_id="cond-A")
        token_b = await repo.list_snapshots(token_id="tok-B")
        since_ms = int((base - timedelta(hours=1)).timestamp() * 1000)
        windowed = await repo.list_snapshots(time_range=TimeRange(since_ms=since_ms))

    assert cond_a.total == 2
    assert token_b.total == 1
    assert token_b.items[0].condition_id == "cond-B"
    assert {s.condition_id for s in windowed.items} == {"cond-A", "cond-B"}
    assert windowed.total == 2  # excludes the old cond-A row
