from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position
from polymarket_trader.extension_api import (
    EntryCandidate,
    ExtensionAction,
    ExtensionContext,
)
from polymarket_trader.domain.account import AccountSnapshot, MarketPause, MarketPauseReason
from strategies.current.config import CurrentStrategyConfig
from strategies.current.strategy import CurrentStrategy, build_strategy
from tests.helpers.markets import build_binary_market


def _no_token_id(market: Market) -> str:
    return market.require_token_id("NO")


def test_current_strategy_exposes_remote_discovery_title_search_queries() -> None:
    strategy = build_strategy()

    queries = strategy.discovery_queries()

    assert [query.name for query in queries] == [
        "title_search:fdv|tag_slug:crypto",
        "title_search:fully diluted valuation|tag_slug:crypto",
    ]
    assert [query.params for query in queries] == [
        {"title_search": "fdv", "tag_slug": "crypto"},
        {"title_search": "fully diluted valuation", "tag_slug": "crypto"},
    ]


def test_current_strategy_can_push_tag_slug_to_remote_discovery_queries() -> None:
    strategy = CurrentStrategy(
        config=CurrentStrategyConfig(
            discovery_title_searches=("fdv",),
            discovery_tag_slugs=("crypto",),
        )
    )

    queries = strategy.discovery_queries()

    assert [query.name for query in queries] == ["title_search:fdv|tag_slug:crypto"]
    assert [query.params for query in queries] == [
        {"title_search": "fdv", "tag_slug": "crypto"},
    ]


def test_current_strategy_can_decide_entry() -> None:
    strategy = build_strategy()
    market = build_binary_market(
        condition_id="condition-1",
        market_slug="slug-1",
        no_token_id="no-1",
        yes_token_id="yes-1",
        event_title="Will token FDV reach a threshold?",
        market_question="Will this project hit $500M FDV?",
        category="Crypto",
        trading_status=TradingStatus.ELIGIBLE,
    )
    orderbook = OrderbookSnapshot(
        token_id="no-1",
        best_bid=Decimal("0.55"),
        best_ask=Decimal("0.60"),
        bids=(PriceLevel(price=Decimal("0.55"), size=Decimal("10")),),
        asks=(PriceLevel(price=Decimal("0.60"), size=Decimal("10")),),
        received_at=datetime.now(timezone.utc),
        market_slug="slug-1",
        condition_id="condition-1",
    )

    universe = strategy.select_market(market)
    decision = strategy.decide_entry(
        ExtensionContext(
            trace_id="trace-1",
            market=market,
            token_id=_no_token_id(market),
            orderbook=orderbook,
            metadata={"amount_usdc": Decimal("25")},
        )
    )

    assert universe.selected is True
    assert decision.action == ExtensionAction.BUY
    assert decision.amount_usdc == Decimal("25")


def test_current_strategy_sizes_entry_from_candidate_snapshots() -> None:
    strategy = build_strategy()
    primary = build_binary_market(
        condition_id="condition-1",
        market_slug="slug-1",
        no_token_id="no-1",
        yes_token_id="yes-1",
        event_title="Will token FDV reach a threshold?",
        market_question="Will this project hit $500M FDV?",
        category="Crypto",
        trading_status=TradingStatus.ELIGIBLE,
    )
    secondary = build_binary_market(
        condition_id="condition-2",
        market_slug="slug-2",
        no_token_id="no-2",
        yes_token_id="yes-2",
        event_title="Will token FDV reach a threshold?",
        market_question="Will this project hit $500M FDV?",
        category="Crypto",
        trading_status=TradingStatus.ELIGIBLE,
    )
    received_at = datetime.now(timezone.utc)
    primary_orderbook = OrderbookSnapshot(
        token_id="no-1",
        best_bid=Decimal("0.55"),
        best_ask=Decimal("0.60"),
        bids=(PriceLevel(price=Decimal("0.55"), size=Decimal("100")),),
        asks=(PriceLevel(price=Decimal("0.60"), size=Decimal("100")),),
        received_at=received_at,
        market_slug="slug-1",
        condition_id="condition-1",
    )
    secondary_orderbook = OrderbookSnapshot(
        token_id="no-2",
        best_bid=Decimal("0.55"),
        best_ask=Decimal("0.60"),
        bids=(PriceLevel(price=Decimal("0.55"), size=Decimal("100")),),
        asks=(PriceLevel(price=Decimal("0.60"), size=Decimal("100")),),
        received_at=received_at,
        market_slug="slug-2",
        condition_id="condition-2",
    )

    sizing = strategy.size_entry(
        ExtensionContext(
            trace_id="trace-sizing",
            market=primary,
            token_id=_no_token_id(primary),
            orderbook=primary_orderbook,
            portfolio_budget_usdc=Decimal("100"),
            available_usdc=Decimal("100"),
            max_order_usdc=Decimal("100"),
            max_market_usdc=Decimal("100"),
            max_total_usdc=Decimal("100"),
            entry_candidates=(
                EntryCandidate(
                    market=primary,
                    token_id=_no_token_id(primary),
                    orderbook=primary_orderbook,
                ),
                EntryCandidate(
                    market=secondary,
                    token_id=_no_token_id(secondary),
                    orderbook=secondary_orderbook,
                ),
            ),
        )
    )

    assert sizing.eligible_market_count == 2
    assert sizing.allocation is not None
    assert sizing.allocation.buy_budget_usdc == Decimal("50")


def test_current_strategy_recovery_returns_target_sell_and_pause_state() -> None:
    strategy = build_strategy()
    market = build_binary_market(
        condition_id="condition-1",
        market_slug="slug-1",
        no_token_id="no-1",
        yes_token_id="yes-1",
        event_title="Will token FDV reach a threshold?",
        market_question="Will this project hit $500M FDV?",
        category="Crypto",
        trading_status=TradingStatus.PAUSED,
    )
    position = Position(
        condition_id="condition-1",
        token_id="no-1",
        shares=Decimal("12"),
        cost_usdc=Decimal("6"),
        market_slug="slug-1",
    )
    account = AccountSnapshot(
        positions=(position,),
        market_pauses=(
            MarketPause.build(
                condition_id="condition-1",
                reason=MarketPauseReason.MANUAL_PAUSE,
            ),
        ),
    )

    recovery = strategy.decide_recovery(
        ExtensionContext(
            trace_id="trace-1",
            market=market,
            account_snapshot=account,
            position=position,
        )
    )

    assert len(recovery.actions) == 1
    assert recovery.actions[0].action == ExtensionAction.SELL
    assert recovery.actions[0].size_shares == Decimal("12")
    assert recovery.pause_trading is True
