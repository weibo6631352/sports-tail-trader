from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api import EntryCandidate, EntrySizing, ExtensionAction, ExtensionContext
from polymarket_trader.domain.account import AccountSnapshot
from strategies.current.strategy import build_strategy as build_current_strategy
from tests.helpers.markets import build_binary_market


def test_strategy_contract_positive_path() -> None:
    strategy = build_current_strategy()
    market = build_binary_market(
        condition_id="condition-current",
        market_slug="slug-current",
        no_token_id="no-current",
        yes_token_id="yes-current",
        event_title="Will token FDV reach a threshold?",
        market_question="Will this project hit $500M FDV?",
        category="Crypto",
        trading_status=TradingStatus.ELIGIBLE,
    )

    received_at = datetime.now(timezone.utc)
    orderbook = OrderbookSnapshot(
        token_id=market.require_token_id("NO"),
        best_bid=Decimal("0.55"),
        best_ask=Decimal("0.60"),
        bids=(PriceLevel(price=Decimal("0.55"), size=Decimal("100")),),
        asks=(PriceLevel(price=Decimal("0.60"), size=Decimal("100")),),
        received_at=received_at,
        market_slug=market.market_slug,
        condition_id=market.condition_id,
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("100"),
        tick_size=market.tick_size,
    )
    position = Position(
        condition_id=market.condition_id,
        token_id=market.require_token_id("NO"),
        shares=Decimal("10"),
        cost_usdc=Decimal("5"),
        market_slug=market.market_slug,
    )
    account_snapshot = AccountSnapshot(
        positions=(position,),
    )

    universe = strategy.select_market(market)
    assert universe.selected is True

    sizing = strategy.size_entry(
        ExtensionContext(
            trace_id="trace-contract",
            market=market,
            token_id=market.require_token_id("NO"),
            orderbook=orderbook,
            entry_candidates=(
                EntryCandidate(
                    market=market,
                    token_id=market.require_token_id("NO"),
                    orderbook=orderbook,
                ),
            ),
            metadata={
                "portfolio_budget_usdc": Decimal("100"),
                "available_usdc": Decimal("100"),
                "max_order_usdc": Decimal("100"),
                "max_market_usdc": Decimal("100"),
                "max_total_usdc": Decimal("100"),
            },
        )
    )
    assert isinstance(sizing, EntrySizing)
    assert sizing.allocation is not None
    assert sizing.allocation.buy_budget_usdc > Decimal("0")

    entry = strategy.decide_entry(
        ExtensionContext(
            trace_id="trace-contract",
            market=market,
            token_id=market.require_token_id("NO"),
            orderbook=orderbook,
            metadata={"amount_usdc": Decimal("10")},
        )
    )
    assert entry.action == ExtensionAction.BUY
    assert entry.amount_usdc == Decimal("10")

    exit_decision = strategy.decide_exit(
        ExtensionContext(
            trace_id="trace-contract",
            market=market,
            position=position,
        )
    )
    assert exit_decision.action == ExtensionAction.SELL

    recovery = strategy.decide_recovery(
        ExtensionContext(
            trace_id="trace-contract",
            market=market,
            position=position,
            account_snapshot=account_snapshot,
        )
    )
    assert len(recovery.actions) == 1
    assert recovery.actions[0].action == ExtensionAction.SELL
    assert recovery.actions[0].size_shares == Decimal("10")
