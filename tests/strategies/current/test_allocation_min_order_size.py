from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from strategies.current.allocation import AllocationMarketSnapshot, equal_weight_plan


def test_allocation_allows_low_usdc_order_when_share_size_meets_market_minimum() -> None:
    plan = equal_weight_plan(
        trace_id="trace-min-share-size",
        portfolio_budget_usdc=Decimal("2"),
        markets=(
            _snapshot(best_ask=Decimal("0.25"), min_order_size=Decimal("5")),
        ),
        available_usdc=Decimal("2"),
        max_order_usdc=Decimal("2"),
        max_market_usdc=Decimal("20"),
        max_total_usdc=Decimal("20"),
    )

    assert plan.reason == ""
    assert plan.allocations[0].buy_budget_usdc == Decimal("2")
    assert plan.allocations[0].release_reason == ""


def test_allocation_rejects_budget_when_converted_share_size_is_below_market_minimum() -> None:
    plan = equal_weight_plan(
        trace_id="trace-below-share-size",
        portfolio_budget_usdc=Decimal("2"),
        markets=(
            _snapshot(best_ask=Decimal("0.60"), min_order_size=Decimal("5")),
        ),
        available_usdc=Decimal("2"),
        max_order_usdc=Decimal("2"),
        max_market_usdc=Decimal("20"),
        max_total_usdc=Decimal("20"),
    )

    assert plan.reason == "no_market_meets_min_order_size"
    assert plan.allocations[0].buy_budget_usdc == Decimal("0")
    assert plan.allocations[0].release_reason == "below_min_order_size"


def _snapshot(*, best_ask: Decimal, min_order_size: Decimal) -> AllocationMarketSnapshot:
    orderbook = OrderbookSnapshot(
        token_id="yes",
        best_bid=best_ask - Decimal("0.01"),
        best_ask=best_ask,
        bids=(PriceLevel(price=best_ask - Decimal("0.01"), size=Decimal("100")),),
        asks=(PriceLevel(price=best_ask, size=Decimal("100")),),
        received_at=datetime.now(timezone.utc),
        condition_id="condition",
        market_slug="market",
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("100"),
        tick_size=Decimal("0.01"),
    )
    return AllocationMarketSnapshot(
        market=Market(
            condition_id="condition",
            market_slug="market",
            outcomes=(
                MarketOutcome(token_id="yes", outcome="Yes"),
                MarketOutcome(token_id="no", outcome="No"),
            ),
            trading_status=TradingStatus.ELIGIBLE,
            min_order_size=min_order_size,
        ),
        token_id="yes",
        orderbook=orderbook,
        best_ask=best_ask,
        best_ask_size=Decimal("100"),
    )
