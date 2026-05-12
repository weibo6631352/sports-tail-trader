"""``MarketRepository`` round-trip / 查询覆盖。

@pytest.mark.pg 用例跑真实 Postgres（``pytest --pg``）。覆盖：
- save → get/list 往返；
- domain ↔ ORM 字段映射（含 fee schedule、tags、tick_size、outcomes 等）；
- ``condition_id`` / ``market_slug`` / ``token_id`` / ``trading_status`` 过滤。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus


def _make_market(
    *,
    condition_id: str = "cond-1",
    market_slug: str = "slug-1",
    outcomes: tuple[MarketOutcome, ...] | None = None,
    trading_status: TradingStatus = TradingStatus.ELIGIBLE,
    **overrides: Any,
) -> Market:
    outcomes = outcomes or (
        MarketOutcome(token_id=f"tok-{condition_id}-yes", outcome="Yes"),
        MarketOutcome(token_id=f"tok-{condition_id}-no", outcome="No"),
    )
    defaults: dict[str, Any] = {
        "condition_id": condition_id,
        "market_slug": market_slug,
        "outcomes": outcomes,
        "market_name": f"name-{condition_id}",
        "event_id": f"evt-{condition_id}",
        "event_title": f"title-{condition_id}",
        "event_slug": f"event-{condition_id}",
        "tick_size": Decimal("0.01"),
        "min_order_size": Decimal("5"),
        "neg_risk": False,
        "fees_enabled": True,
        "maker_base_fee_bps": 10,
        "taker_base_fee_bps": 20,
        "fee_rate_bps": 20,
        "category": "sports",
        "tags": ("nfl", "live"),
        "matched_keywords": ("Patriots",),
        "trading_status": trading_status,
    }
    defaults.update(overrides)
    return Market(**defaults)


@pytest.mark.pg
async def test_market_repository_save_and_fetch_round_trip(pg_session_factory: Any) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import MarketRepository
    from polymarket_trader.infra.db.models import MarketModel

    market = _make_market()

    async with pg_session_factory() as session:
        await session.execute(delete(MarketModel))
        repo = MarketRepository(session)
        await repo.save_market(market, trace_id="trace-1", source="discovery")
        await session.commit()

    async with pg_session_factory() as session:
        repo = MarketRepository(session)
        by_condition = await repo.get_by_condition_id(market.condition_id)
        by_slug = await repo.get_by_market_slug(market.market_slug)
        by_token = await repo.get_by_token_id(market.outcomes[0].token_id)

    assert by_condition is not None
    assert by_condition.condition_id == market.condition_id
    assert by_condition.market_slug == market.market_slug
    assert by_condition.tick_size == Decimal("0.01")
    assert by_condition.fees_enabled is True
    assert by_condition.maker_base_fee_bps == 10
    assert by_condition.taker_base_fee_bps == 20
    assert by_condition.tags == ("nfl", "live")
    assert by_condition.trading_status == TradingStatus.ELIGIBLE
    assert {o.token_id for o in by_condition.outcomes} == {o.token_id for o in market.outcomes}
    assert by_slug is not None and by_slug.condition_id == market.condition_id
    assert by_token is not None and by_token.condition_id == market.condition_id


@pytest.mark.pg
async def test_market_repository_upsert_overrides_existing_row(pg_session_factory: Any) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import MarketRepository
    from polymarket_trader.infra.db.models import MarketModel

    original = _make_market(trading_status=TradingStatus.CANDIDATE)
    updated = _make_market(
        trading_status=TradingStatus.REJECTED,
        reject_reason="missing_outcome",
        fee_rate_bps=33,
    )

    async with pg_session_factory() as session:
        await session.execute(delete(MarketModel))
        repo = MarketRepository(session)
        await repo.save_market(original)
        await repo.save_market(updated)
        await session.commit()

    async with pg_session_factory() as session:
        repo = MarketRepository(session)
        fetched = await repo.get_by_condition_id(original.condition_id)

    assert fetched is not None
    assert fetched.trading_status == TradingStatus.REJECTED
    assert fetched.reject_reason == "missing_outcome"
    assert fetched.fee_rate_bps == 33


@pytest.mark.pg
async def test_market_repository_list_filters_by_trading_status_and_returns_pagination(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import MarketRepository
    from polymarket_trader.infra.db.models import MarketModel

    markets = [
        _make_market(condition_id="cond-A", market_slug="slug-A", trading_status=TradingStatus.ELIGIBLE),
        _make_market(condition_id="cond-B", market_slug="slug-B", trading_status=TradingStatus.ELIGIBLE),
        _make_market(condition_id="cond-C", market_slug="slug-C", trading_status=TradingStatus.REJECTED),
    ]

    async with pg_session_factory() as session:
        await session.execute(delete(MarketModel))
        repo = MarketRepository(session)
        await repo.save_markets(markets)
        await session.commit()

    async with pg_session_factory() as session:
        repo = MarketRepository(session)
        eligible = await repo.list_markets_snapshot(trading_status="eligible")
        rejected = await repo.list_markets_snapshot(trading_status="rejected")
        batch = await repo.list_by_condition_ids(["cond-A", "cond-C", "missing"])

    assert eligible.total == 2
    assert {m.condition_id for m in eligible.items} == {"cond-A", "cond-B"}
    assert rejected.total == 1
    assert rejected.items[0].condition_id == "cond-C"
    assert {m.condition_id for m in batch} == {"cond-A", "cond-C"}


@pytest.mark.pg
async def test_market_repository_list_filters_by_fee_rate_window(
    pg_session_factory: Any,
) -> None:
    from sqlalchemy import delete

    from polymarket_trader.infra.db import MarketRepository
    from polymarket_trader.infra.db.models import MarketModel

    markets = [
        _make_market(condition_id="cond-low", market_slug="slug-low", fee_rate_bps=5),
        _make_market(condition_id="cond-mid", market_slug="slug-mid", fee_rate_bps=25),
        _make_market(condition_id="cond-high", market_slug="slug-high", fee_rate_bps=100),
    ]

    async with pg_session_factory() as session:
        await session.execute(delete(MarketModel))
        repo = MarketRepository(session)
        await repo.save_markets(markets)
        await session.commit()

    async with pg_session_factory() as session:
        repo = MarketRepository(session)
        page = await repo.list_markets_snapshot(fee_rate_bps_min=10, fee_rate_bps_max=50)

    assert {m.condition_id for m in page.items} == {"cond-mid"}
