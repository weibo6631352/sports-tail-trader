from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.runtime.registry import MarketRegistry
from tests.helpers.markets import build_binary_market


def _market(
    *,
    condition_id: str = "condition",
    market_slug: str = "sample-market-a",
    no_token_id: str = "no",
    event_slug: str = "token-event",
) -> Market:
    return build_binary_market(
        condition_id=condition_id,
        market_slug=market_slug,
        no_token_id=no_token_id,
        yes_token_id="yes",
        event_id="event-id",
        event_title="Will this market reach a threshold?",
        event_slug=event_slug,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        category="Crypto",
        matched_keywords=("sample", "market", "threshold"),
        trading_status=TradingStatus.ELIGIBLE,
    )


def test_registry_snapshot_and_indexes_follow_latest_market_state() -> None:
    registry = MarketRegistry()
    market = _market()

    registry.upsert(market)

    assert registry.get_by_condition_id(market.condition_id) == market
    assert registry.get_by_token_id(market.require_token_id("NO")) == market
    assert registry.get_by_slug(market.market_slug) == market
    assert registry.get_by_slug(market.event_slug or "") == market
    assert registry.snapshot().get_by_condition_id(market.condition_id) == market


def test_registry_updates_indexes_after_token_and_slug_change() -> None:
    registry = MarketRegistry()
    original = _market()
    updated = _market(
        no_token_id="no-v2",
        market_slug="sample-market-a-v2",
        event_slug="token-event-v2",
    )

    registry.upsert(original)
    registry.reconcile(updated)

    assert registry.get_by_token_id("no") is None
    assert registry.get_by_slug("sample-market-a") is None
    assert registry.get_by_slug("token-event") is None
    assert registry.get_by_token_id(updated.require_token_id("NO")) == updated
    assert registry.get_by_slug(updated.market_slug) == updated
    assert registry.get_by_slug(updated.event_slug or "") == updated


def test_registry_updates_fee_schedule_and_fee_rate() -> None:
    registry = MarketRegistry()
    market = _market()
    registry.upsert(market)

    fee_updated_at = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
    registry.update_fee_schedule(
        market.condition_id,
        fees_enabled=True,
        maker_base_fee_bps=0,
        taker_base_fee_bps=100,
    )
    registry.update_fee_rate(
        market.condition_id,
        125,
        fee_rate_updated_at=fee_updated_at,
    )

    updated = registry.get_by_condition_id(market.condition_id)
    assert updated is not None
    assert updated.fees_enabled is True
    assert updated.maker_base_fee_bps == 0
    assert updated.taker_base_fee_bps == 100
    assert updated.fee_rate_bps == 125
    assert updated.fee_rate_updated_at == fee_updated_at
