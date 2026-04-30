from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import BuyOrderIntent, Order, OrderSide, OrderStatus, OrderType, SellOrderIntent
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
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


def test_sell_exit_can_reduce_position_when_local_market_is_only_candidate() -> None:
    decision = RiskManager().check_order_intent(
        SellOrderIntent(
            trace_id="trace-candidate-sell-exit",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.99"),
            size_shares=Decimal("3"),
        ),
        market=_market().with_trading_status(TradingStatus.CANDIDATE),
        position=Position(
            condition_id="condition",
            token_id="yes",
            shares=Decimal("3"),
            cost_usdc=Decimal("2.97"),
        ),
        market_active=False,
        market_open=False,
        clob_enabled=True,
    )

    assert decision.passed is True


def test_sell_exit_allows_exchange_endpoint_price_on_market_tick_grid() -> None:
    decision = RiskManager().check_order_intent(
        SellOrderIntent(
            trace_id="trace-sell-endpoint-price",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.99"),
            size_shares=Decimal("3"),
        ),
        market=_market().with_tick_size(Decimal("0.01")),
        position=Position(
            condition_id="condition",
            token_id="yes",
            shares=Decimal("3"),
            cost_usdc=Decimal("2.97"),
        ),
    )

    assert decision.passed is True


def test_sell_exit_rejects_price_above_exchange_tick_limit() -> None:
    decision = RiskManager().check_order_intent(
        SellOrderIntent(
            trace_id="trace-sell-above-endpoint-price",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.999"),
            size_shares=Decimal("3"),
        ),
        market=_market().with_tick_size(Decimal("0.01")),
        position=Position(
            condition_id="condition",
            token_id="yes",
            shares=Decimal("3"),
            cost_usdc=Decimal("2.97"),
        ),
    )

    assert decision.passed is False
    assert decision.reason == "price_above_tick_limit"


def test_buy_entry_still_rejects_when_local_market_is_only_candidate() -> None:
    decision = RiskManager().check_order_intent(
        BuyOrderIntent(
            trace_id="trace-candidate-buy-entry",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.99"),
            amount_usdc=Decimal("3"),
        ),
        market=_market().with_trading_status(TradingStatus.CANDIDATE),
        market_active=False,
        market_open=False,
        clob_enabled=True,
        balance_usdc=Decimal("10"),
        allowance_usdc=Decimal("10"),
    )

    assert decision.passed is False
    assert decision.reason == "market_not_active"


def test_buy_min_order_uses_share_size_not_usdc_amount() -> None:
    decision = RiskManager().check_order_intent(
        BuyOrderIntent(
            trace_id="trace-buy-below-five-usdc",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.25"),
            amount_usdc=Decimal("2"),
        ),
        market=_market(min_order_size=Decimal("5")),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("10"),
        max_total_usdc=Decimal("10"),
        balance_usdc=Decimal("10"),
        allowance_usdc=Decimal("10"),
    )

    assert decision.passed is True
    min_order_check = next(check for check in decision.checks if check.name == "min_order_gate")
    assert min_order_check.value["order_size_shares"] == Decimal("8")


def test_buy_min_order_rejects_when_converted_share_size_is_too_small() -> None:
    decision = RiskManager().check_order_intent(
        BuyOrderIntent(
            trace_id="trace-buy-small-share-size",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.50"),
            amount_usdc=Decimal("2"),
        ),
        market=_market(min_order_size=Decimal("5")),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("10"),
        max_total_usdc=Decimal("10"),
        balance_usdc=Decimal("10"),
        allowance_usdc=Decimal("10"),
    )

    assert decision.passed is False
    assert decision.reason == "min_order_not_met"
    assert decision.failed_field == "intent.amount_usdc/intent.price"
    assert decision.checks[-1].value["order_size_shares"] == Decimal("4")


def test_buy_gtc_post_only_maker_bid_does_not_require_taker_ask_depth() -> None:
    decision = RiskManager().check_order_intent(
        BuyOrderIntent(
            trace_id="trace-maker-bid-liquidity",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.995"),
            amount_usdc=Decimal("4.995"),
            order_type=OrderType.GTC,
            post_only=True,
        ),
        market=_market(min_order_size=Decimal("5")).with_tick_size(Decimal("0.001")),
        orderbook=OrderbookSnapshot(
            token_id="yes",
            condition_id="condition",
            best_bid=Decimal("0.991"),
            best_ask=Decimal("0.999"),
            bids=(PriceLevel(price=Decimal("0.991"), size=Decimal("55")),),
            asks=(PriceLevel(price=Decimal("0.999"), size=Decimal("450.68")),),
            received_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        ),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("10"),
        max_total_usdc=Decimal("10"),
        balance_usdc=Decimal("10"),
        allowance_usdc=Decimal("10"),
    )

    assert decision.passed is True
    assert all(check.reason != "liquidity_insufficient" for check in decision.checks)


def test_buy_taker_rejects_when_balance_cannot_cover_fee_estimate() -> None:
    decision = RiskManager().check_order_intent(
        BuyOrderIntent(
            trace_id="trace-buy-fee-balance",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.001"),
            amount_usdc=Decimal("4.4261"),
        ),
        market=_market(min_order_size=Decimal("5")).with_tick_size(Decimal("0.001")).with_fee_rate(30),
        orderbook=OrderbookSnapshot(
            token_id="yes",
            condition_id="condition",
            best_bid=Decimal("0"),
            best_ask=Decimal("0.001"),
            bids=(),
            asks=(PriceLevel(price=Decimal("0.001"), size=Decimal("10000")),),
            received_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        ),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("10"),
        max_total_usdc=Decimal("10"),
        balance_usdc=Decimal("4.4261"),
        allowance_usdc=Decimal("10"),
    )

    assert decision.passed is False
    assert decision.reason == "balance_insufficient"
    assert decision.failed_field == "account.balance_usdc"
    assert decision.checks[-1].value["estimated_fee_usdc"] > Decimal("0")


def test_buy_rejects_dust_notional_below_clob_floor() -> None:
    decision = RiskManager().check_order_intent(
        BuyOrderIntent(
            trace_id="trace-buy-dust",
            condition_id="condition",
            token_id="yes",
            price=Decimal("0.001"),
            amount_usdc=Decimal("0.0073"),
        ),
        market=_market(min_order_size=Decimal("5")).with_tick_size(Decimal("0.001")),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("10"),
        max_total_usdc=Decimal("10"),
        balance_usdc=Decimal("1"),
        allowance_usdc=Decimal("1"),
    )

    assert decision.passed is False
    assert decision.reason == "min_notional_not_met"
    assert decision.failed_field == "intent.amount_usdc"


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


def _market(*, min_order_size: Decimal = Decimal("1")) -> Market:
    return Market(
        condition_id="condition",
        market_slug="virtual-market",
        outcomes=(
            MarketOutcome(token_id="yes", outcome="Yes"),
            MarketOutcome(token_id="no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
        tick_size=Decimal("0.01"),
        min_order_size=min_order_size,
    )
