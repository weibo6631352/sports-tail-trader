from __future__ import annotations

from decimal import Decimal

from datetime import datetime, timezone

from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from strategies.current.allocation import AllocationMarketSnapshot, equal_weight_budget, equal_weight_plan
from tests.helpers.markets import build_binary_market


def _market(condition_id: str, market_slug: str, no_token_id: str) -> Market:
    return build_binary_market(
        condition_id=condition_id,
        market_slug=market_slug,
        no_token_id=no_token_id,
        yes_token_id=f"yes-{no_token_id}",
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        category="Crypto",
        matched_keywords=("sample", "market", "threshold"),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _snapshot(market: Market) -> AllocationMarketSnapshot:
    orderbook = OrderbookSnapshot(
        token_id=market.require_token_id("NO"),
        best_bid=Decimal("0.55"),
        best_ask=Decimal("0.43"),
        bids=(PriceLevel(price=Decimal("0.55"), size=Decimal("100")),),
        asks=(PriceLevel(price=Decimal("0.43"), size=Decimal("100")),),
        received_at=datetime.now(timezone.utc),
        market_slug=market.market_slug,
        condition_id=market.condition_id,
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("100"),
        tick_size=market.tick_size,
    )
    return AllocationMarketSnapshot(
        market=market,
        token_id=market.require_token_id("NO"),
        orderbook=orderbook,
        tradable=True,
        market_active=True,
        market_open=True,
        clob_enabled=True,
        best_ask=Decimal("0.43"),
        liquidity_usdc=Decimal("60"),
        spread=Decimal("0.05"),
    )


def test_equal_weight_budget_returns_zero_without_eligible_markets() -> None:
    assert equal_weight_budget(Decimal("100"), 0) == Decimal("0")


def test_equal_weight_plan_respects_per_market_cap_and_releases_budget() -> None:
    first = _snapshot(_market("condition-1", "token-1", "no-1"))
    second = _snapshot(_market("condition-2", "token-2", "no-2"))

    plan = equal_weight_plan(
        trace_id="trace",
        portfolio_budget_usdc=Decimal("100"),
        markets=(first, second),
        available_usdc=Decimal("100"),
        max_order_usdc=Decimal("100"),
        max_market_usdc=Decimal("20"),
        max_total_usdc=Decimal("100"),
    )

    assert plan.eligible_market_count == 2
    assert all(allocation.buy_budget_usdc <= Decimal("20") for allocation in plan.allocations)
    assert plan.released_budget_usdc == Decimal("60")
