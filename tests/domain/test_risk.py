from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import BuyOrderIntent, Order, OrderSide, OrderStatus, OrderType, SellOrderIntent
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.risk import RiskManager


def test_buy_entry_respects_budget_exposure_limits() -> None:
    decision = RiskManager().check_order_intent(
        BuyOrderIntent(
            trace_id="trace-buy-budget",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.99"),
            amount_usdc=Decimal("3"),
        ),
        market=_market(),
        max_order_usdc=Decimal("1"),
        max_market_usdc=Decimal("1"),
        max_total_usdc=Decimal("1"),
        balance_usdc=Decimal("0"),
        allowance_usdc=Decimal("0"),
    )

    assert decision.passed is False
    assert decision.reason == "single_order_limit_reached"


def test_sell_exit_does_not_consume_buy_budget_or_balance() -> None:
    decision = RiskManager().check_order_intent(
        SellOrderIntent(
            trace_id="trace-sell-exit",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.99"),
            size_shares=Decimal("3"),
        ),
        market=_market(),
        position=Position(
            condition_id="condition",
            token_id="yes",
            shares=Decimal("3"),
            cost_usdc=Decimal("3"),
        ),
        max_order_usdc=Decimal("1"),
        max_market_usdc=Decimal("1"),
        max_total_usdc=Decimal("1"),
        balance_usdc=Decimal("0"),
        allowance_usdc=Decimal("0"),
    )

    assert decision.passed is True


def test_buy_entry_rejects_when_exit_order_is_already_open_for_same_token() -> None:
    decision = RiskManager().check_order_intent(
        BuyOrderIntent(
            trace_id="trace-buy-while-exit-open",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.90"),
            amount_usdc=Decimal("3"),
        ),
        market=_market(),
        open_orders=(
            Order(
                trace_id="trace-exit",
                condition_id="condition",
                token_id="yes",
                side=OrderSide.SELL,
                order_type=OrderType.GTC,
                price=Decimal("0.995"),
                size_shares=Decimal("3"),
                status=OrderStatus.LIVE,
                order_id="exit-order",
            ),
        ),
        max_order_usdc=Decimal("5"),
        max_market_usdc=Decimal("5"),
        max_total_usdc=Decimal("5"),
        balance_usdc=Decimal("10"),
        allowance_usdc=Decimal("10"),
    )

    assert decision.passed is False
    assert decision.reason == "open_exit_detected"
    assert decision.suggested_action == "wait_exit"


def test_controlled_scale_in_buy_can_pass_open_exit_gate_when_explicitly_allowed() -> None:
    decision = RiskManager().check_order_intent(
        BuyOrderIntent(
            trace_id="trace-scale-in",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.90"),
            amount_usdc=Decimal("3"),
            allow_open_exit_overlap=True,
        ),
        market=_market(),
        position=Position(
            condition_id="condition",
            token_id="yes",
            shares=Decimal("4"),
            cost_usdc=Decimal("2"),
            open_sell_shares=Decimal("4"),
        ),
        open_orders=(
            Order(
                trace_id="trace-exit",
                condition_id="condition",
                token_id="yes",
                side=OrderSide.SELL,
                order_type=OrderType.GTC,
                price=Decimal("0.995"),
                size_shares=Decimal("4"),
                remaining_shares=Decimal("4"),
                status=OrderStatus.LIVE,
                order_id="exit-order",
            ),
        ),
        max_order_usdc=Decimal("5"),
        max_market_usdc=Decimal("20"),
        max_total_usdc=Decimal("20"),
        balance_usdc=Decimal("10"),
        allowance_usdc=Decimal("10"),
    )

    assert decision.passed is True


def _market() -> Market:
    return Market(
        condition_id="condition",
        market_slug="virtual-market",
        outcomes=(
            MarketOutcome(token_id="yes", outcome="Yes"),
            MarketOutcome(token_id="no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
    )
