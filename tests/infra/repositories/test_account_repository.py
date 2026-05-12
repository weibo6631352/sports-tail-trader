"""``AccountSnapshotRepository`` round-trip / 查询覆盖。

PG 用例验证 append-only 时间序列写入、最新快照查询、
``query_history_bucketed`` 服务端 downsampling。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from polymarket_trader.domain.account import AccountSnapshot


def _make_snapshot(
    *,
    balance_usdc: Decimal = Decimal("1000"),
    allowance_usdc: Decimal = Decimal("5000"),
    user_ws_connected: bool = True,
    allow_new_entries: bool = True,
) -> AccountSnapshot:
    return AccountSnapshot(
        balance_usdc=balance_usdc,
        allowance_usdc=allowance_usdc,
        user_ws_connected=user_ws_connected,
        allow_new_entries=allow_new_entries,
    )


@pytest.mark.pg
async def test_account_repository_save_and_get_current(pg_session_factory: Any) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import AccountSnapshotRepository
    from polymarket_trader.infra.db.models import AccountSnapshotModel

    snapshot = _make_snapshot()
    recorded = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)

    async with pg_session_factory() as session:
        await session.execute(delete(AccountSnapshotModel))
        repo = AccountSnapshotRepository(session)
        await repo.save_snapshot(
            snapshot,
            trace_id="trace-1",
            recorded_at=recorded,
            net_value_usdc=Decimal("1500"),
        )
        await session.commit()

    async with pg_session_factory() as session:
        repo = AccountSnapshotRepository(session)
        current = await repo.get_current_snapshot()

    assert current is not None
    assert current.balance_usdc == Decimal("1000")
    assert current.allowance_usdc == Decimal("5000")
    assert current.user_ws_connected is True
    assert current.allow_new_entries is True


@pytest.mark.pg
async def test_account_repository_append_only_returns_latest_row(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete, func, select

    from polymarket_trader.infra.db import AccountSnapshotRepository
    from polymarket_trader.infra.db.models import AccountSnapshotModel

    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    old = _make_snapshot(balance_usdc=Decimal("100"))
    new = _make_snapshot(balance_usdc=Decimal("999"))

    async with pg_session_factory() as session:
        await session.execute(delete(AccountSnapshotModel))
        repo = AccountSnapshotRepository(session)
        await repo.save_snapshot(old, recorded_at=base - timedelta(hours=1), net_value_usdc=Decimal("100"))
        await repo.save_snapshot(new, recorded_at=base, net_value_usdc=Decimal("999"))
        await session.commit()

    async with pg_session_factory() as session:
        total = await session.scalar(select(func.count(AccountSnapshotModel.id)))
        repo = AccountSnapshotRepository(session)
        latest = await repo.get_current_snapshot()

    # append-only：两行都保留；get_current 返回 recorded_at 最新
    assert total == 2
    assert latest is not None and latest.balance_usdc == Decimal("999")


@pytest.mark.pg
async def test_account_repository_history_bucketed_downsamples_by_interval(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import AccountSnapshotRepository
    from polymarket_trader.infra.db.models import AccountSnapshotModel

    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    # 三条记录：12:00、12:00:30、12:05。1 分钟 bucket 应得 2 个点。
    snapshots = [
        (base, Decimal("100")),
        (base + timedelta(seconds=30), Decimal("110")),
        (base + timedelta(minutes=5), Decimal("120")),
    ]

    async with pg_session_factory() as session:
        await session.execute(delete(AccountSnapshotModel))
        repo = AccountSnapshotRepository(session)
        for recorded_at, net_value in snapshots:
            await repo.save_snapshot(
                _make_snapshot(),
                recorded_at=recorded_at,
                net_value_usdc=net_value,
            )
        await session.commit()

    async with pg_session_factory() as session:
        repo = AccountSnapshotRepository(session)
        points = await repo.query_history_bucketed(
            since=base - timedelta(seconds=1),
            until=base + timedelta(minutes=10),
            interval_ms=60_000,
        )

    assert len(points) == 2
    # 第一个 bucket 内取 recorded_at 最大的（12:00:30 / 110）；第二个 bucket 是 12:05 / 120。
    assert points[0].net_value_usdc == Decimal("110")
    assert points[1].net_value_usdc == Decimal("120")


@pytest.mark.pg
async def test_account_repository_history_rejects_non_positive_interval(
    pg_session_factory: Any,
) -> None:
    from polymarket_trader.infra.db import AccountSnapshotRepository

    base = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    async with pg_session_factory() as session:
        repo = AccountSnapshotRepository(session)
        with pytest.raises(ValueError):
            await repo.query_history_bucketed(
                since=base,
                until=base + timedelta(minutes=1),
                interval_ms=0,
            )
