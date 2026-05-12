"""``PositionRepository`` round-trip / 查询覆盖。

PG 用例验证 ``position_key=(condition_id|token_id)`` upsert、Decimal
字段映射、按 condition/strategy/token 过滤及 ``list_by_condition_ids``
批量查询。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from polymarket_trader.domain.position import Position


def _make_position(
    *,
    condition_id: str = "cond-1",
    token_id: str = "tok-1",
    strategy_id: str = "sports_tail",
    shares: Decimal = Decimal("200"),
    cost_usdc: Decimal = Decimal("100"),
    open_buy_shares: Decimal = Decimal("0"),
    open_sell_shares: Decimal = Decimal("0"),
    avg_price: Decimal | None = Decimal("0.50"),
    current_value: Decimal | None = Decimal("110"),
    cash_pnl: Decimal | None = Decimal("10"),
    redeemable: bool | None = False,
) -> Position:
    return Position(
        strategy_id=strategy_id,
        condition_id=condition_id,
        token_id=token_id,
        shares=shares,
        cost_usdc=cost_usdc,
        market_slug=f"slug-{condition_id}",
        open_buy_shares=open_buy_shares,
        open_sell_shares=open_sell_shares,
        avg_price=avg_price,
        current_value=current_value,
        cash_pnl=cash_pnl,
        redeemable=redeemable,
    )


@pytest.mark.pg
async def test_position_repository_save_and_round_trip(pg_session_factory: Any) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import PositionRepository
    from polymarket_trader.infra.db.models import PositionModel

    position = _make_position()

    async with pg_session_factory() as session:
        await session.execute(delete(PositionModel))
        repo = PositionRepository(session)
        await repo.save_position(position, trace_id="trace-1")
        await session.commit()

    async with pg_session_factory() as session:
        repo = PositionRepository(session)
        page = await repo.list_positions_snapshot(condition_id=position.condition_id)

    assert page.total == 1
    restored = page.items[0]
    assert restored.condition_id == position.condition_id
    assert restored.token_id == position.token_id
    assert restored.strategy_id == position.strategy_id
    assert restored.shares == Decimal("200")
    assert restored.cost_usdc == Decimal("100")
    assert restored.avg_price == Decimal("0.50")
    assert restored.current_value == Decimal("110")
    assert restored.cash_pnl == Decimal("10")
    assert restored.redeemable is False


@pytest.mark.pg
async def test_position_repository_upsert_by_condition_token_key(pg_session_factory: Any) -> None:
    from sqlalchemy import delete, func, select

    from polymarket_trader.infra.db import PositionRepository
    from polymarket_trader.infra.db.models import PositionModel

    first = _make_position(shares=Decimal("100"), cost_usdc=Decimal("50"))
    second = _make_position(shares=Decimal("250"), cost_usdc=Decimal("125"))

    async with pg_session_factory() as session:
        await session.execute(delete(PositionModel))
        repo = PositionRepository(session)
        await repo.save_position(first)
        await repo.save_position(second)
        await session.commit()

    async with pg_session_factory() as session:
        total = await session.scalar(select(func.count(PositionModel.id)))
        repo = PositionRepository(session)
        page = await repo.list_positions_snapshot(condition_id="cond-1")

    assert total == 1
    assert page.items[0].shares == Decimal("250")
    assert page.items[0].cost_usdc == Decimal("125")


@pytest.mark.pg
async def test_position_repository_filters_by_token_and_strategy(pg_session_factory: Any) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import PositionRepository
    from polymarket_trader.infra.db.models import PositionModel

    a = _make_position(condition_id="cond-A", token_id="tok-A")
    b = _make_position(condition_id="cond-B", token_id="tok-B")
    other = _make_position(condition_id="cond-C", token_id="tok-C", strategy_id="other")

    async with pg_session_factory() as session:
        await session.execute(delete(PositionModel))
        repo = PositionRepository(session)
        await repo.save_positions([a, b, other])
        await session.commit()

    async with pg_session_factory() as session:
        repo = PositionRepository(session)
        by_token = await repo.list_positions_snapshot(token_id="tok-B")
        by_strategy = await repo.list_positions_snapshot(strategy_id="other")

    assert by_token.total == 1 and by_token.items[0].token_id == "tok-B"
    assert by_strategy.total == 1 and by_strategy.items[0].condition_id == "cond-C"


@pytest.mark.pg
async def test_position_repository_list_by_condition_ids_batches_lookup(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import PositionRepository
    from polymarket_trader.infra.db.models import PositionModel

    seeds = [
        _make_position(condition_id=cid, token_id=f"tok-{cid}")
        for cid in ("cond-A", "cond-B", "cond-C")
    ]

    async with pg_session_factory() as session:
        await session.execute(delete(PositionModel))
        repo = PositionRepository(session)
        await repo.save_positions(seeds)
        await session.commit()

    async with pg_session_factory() as session:
        repo = PositionRepository(session)
        batch = await repo.list_by_condition_ids(["cond-A", "cond-C", "missing"])
        empty = await repo.list_by_condition_ids([])

    assert {p.condition_id for p in batch} == {"cond-A", "cond-C"}
    assert empty == ()
