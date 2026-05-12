"""``AllocationRepository`` round-trip / 查询覆盖。

PG 用例验证 ``allocation_key``（idempotency_key 或 trace+condition）
upsert、域 ↔ ORM Decimal 字段映射、``trace_id`` / ``condition_id`` /
``strategy_id`` 过滤。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from polymarket_trader.domain.allocation import Allocation


def _make_allocation(
    *,
    condition_id: str = "cond-1",
    market_slug: str = "slug-1",
    token_id: str = "tok-1",
    strategy_id: str = "sports_tail",
    target_budget_usdc: Decimal = Decimal("100"),
    buy_budget_usdc: Decimal = Decimal("80"),
    idempotency_key: str | None = None,
    reason: str = "rebalance",
) -> Allocation:
    return Allocation(
        strategy_id=strategy_id,
        condition_id=condition_id,
        market_slug=market_slug,
        token_id=token_id,
        target_budget_usdc=target_budget_usdc,
        buy_budget_usdc=buy_budget_usdc,
        current_exposure_usdc=Decimal("10.5"),
        released_budget_usdc=Decimal("0"),
        reason=reason,
        idempotency_key=idempotency_key,
        release_reason="",
    )


@pytest.mark.pg
async def test_allocation_repository_save_and_round_trip(pg_session_factory: Any) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import AllocationRepository
    from polymarket_trader.infra.db.models import AllocationModel

    allocation = _make_allocation(idempotency_key="alloc-key-1")

    async with pg_session_factory() as session:
        await session.execute(delete(AllocationModel))
        repo = AllocationRepository(session)
        await repo.save_allocation(allocation, trace_id="trace-1")
        await session.commit()

    async with pg_session_factory() as session:
        repo = AllocationRepository(session)
        page = await repo.list_allocations_snapshot(condition_id=allocation.condition_id)

    assert page.total == 1
    restored = page.items[0]
    assert restored.strategy_id == allocation.strategy_id
    assert restored.condition_id == allocation.condition_id
    assert restored.market_slug == allocation.market_slug
    assert restored.token_id == allocation.token_id
    assert restored.target_budget_usdc == Decimal("100")
    assert restored.buy_budget_usdc == Decimal("80")
    assert restored.current_exposure_usdc == Decimal("10.5")
    assert restored.idempotency_key == "alloc-key-1"
    assert restored.reason == "rebalance"


@pytest.mark.pg
async def test_allocation_repository_upsert_by_idempotency_key(pg_session_factory: Any) -> None:
    from sqlalchemy import delete, func, select

    from polymarket_trader.infra.db import AllocationRepository
    from polymarket_trader.infra.db.models import AllocationModel

    first = _make_allocation(idempotency_key="alloc-key-dup", buy_budget_usdc=Decimal("80"))
    second = _make_allocation(idempotency_key="alloc-key-dup", buy_budget_usdc=Decimal("200"))

    async with pg_session_factory() as session:
        await session.execute(delete(AllocationModel))
        repo = AllocationRepository(session)
        await repo.save_allocation(first, trace_id="trace-1")
        await repo.save_allocation(second, trace_id="trace-1")
        await session.commit()

    async with pg_session_factory() as session:
        total = await session.scalar(select(func.count(AllocationModel.id)))
        repo = AllocationRepository(session)
        page = await repo.list_allocations_snapshot(trace_id="trace-1")

    assert total == 1
    assert page.items[0].buy_budget_usdc == Decimal("200")


@pytest.mark.pg
async def test_allocation_repository_filters_by_trace_strategy_and_market(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import AllocationRepository
    from polymarket_trader.infra.db.models import AllocationModel

    a = _make_allocation(
        condition_id="cond-A",
        market_slug="slug-A",
        token_id="tok-A",
        idempotency_key="alloc-A",
    )
    b = _make_allocation(
        condition_id="cond-B",
        market_slug="slug-B",
        token_id="tok-B",
        idempotency_key="alloc-B",
        strategy_id="other_strategy",
    )

    async with pg_session_factory() as session:
        await session.execute(delete(AllocationModel))
        repo = AllocationRepository(session)
        await repo.save_allocation(a, trace_id="trace-A")
        await repo.save_allocation(b, trace_id="trace-B")
        await session.commit()

    async with pg_session_factory() as session:
        repo = AllocationRepository(session)
        by_trace = await repo.list_allocations_snapshot(trace_id="trace-A")
        by_strategy = await repo.list_allocations_snapshot(strategy_id="other_strategy")
        by_slug = await repo.list_allocations_snapshot(market_slug="slug-A")

    assert by_trace.total == 1 and by_trace.items[0].condition_id == "cond-A"
    assert by_strategy.total == 1 and by_strategy.items[0].condition_id == "cond-B"
    assert by_slug.total == 1 and by_slug.items[0].condition_id == "cond-A"
