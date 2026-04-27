from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.domain.order import (
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderStatus,
    OrderType,
)
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.infra.polymarket.order_executor import (
    InMemoryPolymarketOrderClient,
    PolymarketOrderExecutor,
)
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.event_bus import EventBus
from polymarket_trader.runtime.registry import MarketRegistry
from strategies.current.strategy import build_strategy
from polymarket_trader.workers.market_ws_worker import MarketWsWorker
from polymarket_trader.workers.trading_decision_worker import TradingDecisionWorker
from tests.helpers.markets import build_binary_market


def _market(
    *,
    condition_id: str,
    token_id: str,
    market_slug: str,
) -> Market:
    return build_binary_market(
        condition_id=condition_id,
        market_slug=market_slug,
        no_token_id=token_id,
        yes_token_id=f"yes-{token_id}",
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        event_title="Will token FDV reach a threshold?",
        market_question="Will this project hit $500M FDV?",
        category="Crypto",
        trading_status=TradingStatus.ELIGIBLE,
    )


def _no_token_id(market: Market) -> str:
    return market.require_token_id("NO")


def _snapshot(
    *,
    market: Market,
    best_bid: str = "0.55",
    best_ask: str = "0.60",
) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id=_no_token_id(market),
        best_bid=Decimal(best_bid),
        best_ask=Decimal(best_ask),
        bids=(PriceLevel(price=Decimal(best_bid), size=Decimal("100")),),
        asks=(PriceLevel(price=Decimal(best_ask), size=Decimal("200")),),
        received_at=datetime.now(timezone.utc),
        market_slug=market.market_slug,
        condition_id=market.condition_id,
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("200"),
        tick_size=market.tick_size,
    )


def _entry_event(market: Market, *, trace_id: str) -> DomainEvent:
    return DomainEvent(
        trace_id=trace_id,
        event_type=DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED,
        event_id=f"{trace_id}-event",
        market_slug=market.market_slug,
        condition_id=market.condition_id,
        token_id=_no_token_id(market),
        reason="orderbook_snapshot_updated",
    )


def _ready_account_state_store() -> AccountStateStore:
    store = AccountStateStore()
    store.update_balances(
        balance_usdc=Decimal("100"),
        allowance_usdc=Decimal("100"),
    )
    store.mark_user_ws_connected(True)
    store.mark_reconciled()
    return store


def _buy_result(
    *,
    market: Market,
    requested_amount_usdc: Decimal,
    status: OrderResultStatus,
    matched_shares: Decimal,
    remaining_shares: Decimal,
    spent_usdc: Decimal,
    reason: str,
) -> OrderResult:
    return OrderResult(
        trace_id="trace-buy",
        condition_id=market.condition_id,
        token_id=_no_token_id(market),
        market_slug=market.market_slug,
        status=status,
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        price=Decimal("0.60"),
        requested_amount_usdc=requested_amount_usdc,
        requested_size_shares=requested_amount_usdc / Decimal("0.60"),
        matched_shares=matched_shares,
        remaining_shares=remaining_shares,
        spent_usdc=spent_usdc,
        notional_usdc=requested_amount_usdc,
        order_id="buy-order",
        trade_id="buy-trade",
        reason=reason,
    )


def _sell_result(
    *,
    market: Market,
    size_shares: Decimal,
    status: OrderResultStatus = OrderResultStatus.FULL_FILL,
    matched_shares: Decimal | None = None,
    remaining_shares: Decimal | None = None,
) -> OrderResult:
    matched_shares = size_shares if matched_shares is None else matched_shares
    remaining_shares = Decimal("0") if remaining_shares is None else remaining_shares
    return OrderResult(
        trace_id="trace-sell",
        condition_id=market.condition_id,
        token_id=_no_token_id(market),
        market_slug=market.market_slug,
        status=status,
        side=OrderSide.SELL,
        order_type=OrderType.GTC,
        price=Decimal("0.70"),
        requested_size_shares=size_shares,
        matched_shares=matched_shares,
        remaining_shares=remaining_shares,
        spent_usdc=size_shares * Decimal("0.70"),
        notional_usdc=size_shares * Decimal("0.70"),
        order_id="sell-order",
        trade_id="sell-trade",
        reason="exit_submitted",
    )


class _ScriptedExecutor:
    def __init__(self, *results: OrderResult) -> None:
        self._results = list(results)
        self.intents = []

    async def submit(self, intent):
        self.intents.append(intent)
        if not self._results:
            raise AssertionError("unexpected submit call")
        return self._results.pop(0)


def test_trading_decision_service_allocates_equally_across_eligible_markets() -> None:
    registry = MarketRegistry()
    strategy = build_strategy()
    primary = _market(condition_id="condition-1", token_id="no-1", market_slug="token-1")
    secondary = _market(condition_id="condition-2", token_id="no-2", market_slug="token-2")
    registry.upsert(primary)
    registry.upsert(secondary)
    snapshots = {
        _no_token_id(primary): _snapshot(market=primary),
        _no_token_id(secondary): _snapshot(market=secondary),
    }
    service = TradingDecisionService(
        extension_hooks=strategy,
        registry=registry,
        orderbook_reader=snapshots.get,
    )

    plan = service.build_entry_plan(
        condition_id=primary.condition_id,
        token_id=_no_token_id(primary),
        trace_id="trace",
        portfolio_budget_usdc=Decimal("100"),
        available_usdc=Decimal("100"),
        max_order_usdc=Decimal("100"),
        max_market_usdc=Decimal("100"),
        max_total_usdc=Decimal("100"),
    )

    assert plan.ready_to_trade
    assert plan.eligible_market_count == 2
    assert plan.allocation is not None
    assert plan.allocation.buy_budget_usdc == Decimal("50")
    assert plan.intent is not None
    assert plan.intent.amount_usdc == Decimal("50")


def test_trading_decision_worker_turns_orderbook_update_into_risk_result() -> None:
    async def run() -> None:
        event_bus = EventBus()
        registry = MarketRegistry()
        account_state_store = _ready_account_state_store()
        market_ws_worker = MarketWsWorker(event_bus=event_bus, registry=registry)
        strategy = build_strategy()
        market = _market(condition_id="condition", token_id="no-token", market_slug="token")
        market_ws_worker.track_market(market)

        trading_decision_service = TradingDecisionService(
            extension_hooks=strategy,
            registry=registry,
            orderbook_reader=market_ws_worker.snapshot,
        )
        executor = PolymarketOrderExecutor(client=InMemoryPolymarketOrderClient())
        trading_decision_worker = TradingDecisionWorker(
            event_bus=event_bus,
            trading_decision_service=trading_decision_service,
            trading_service=TradingService(executor=executor),
            account_state_store=account_state_store,
            portfolio_budget_usdc=Decimal("100"),
            available_usdc=Decimal("100"),
            max_order_usdc=Decimal("100"),
            max_market_usdc=Decimal("100"),
            max_total_usdc=Decimal("100"),
            balance_usdc=Decimal("100"),
            allowance_usdc=Decimal("100"),
            max_open_orders=10,
            order_retry_limit=2,
        )

        await market_ws_worker.handle_message(
            {
                "type": "best_bid_ask",
                "token_id": _no_token_id(market),
                "best_bid": "0.55",
                "best_ask": "0.60",
                "best_bid_size": "100",
                "best_ask_size": "200",
            }
        )
        entry_event = await event_bus.next_trading_event()
        result = await trading_decision_worker.process_event(entry_event)

        assert result is not None
        assert result.plan.ready_to_trade
        assert result.review is not None
        assert result.review.risk_decision.passed
        assert result.emitted_event.event_type == DomainEventType.RISK_CHECK_PASSED
        assert result.review.order_result is not None
        assert result.review.order_result.status == OrderResultStatus.NO_FILL

    asyncio.run(run())


def test_trading_decision_worker_partial_fill_only_sells_filled_shares() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        account_state_store = _ready_account_state_store()
        strategy = build_strategy()
        market = _market(condition_id="condition", token_id="no-token", market_slug="token")
        registry.upsert(market)
        trading_decision_service = TradingDecisionService(
            extension_hooks=strategy,
            registry=registry,
            orderbook_reader={_no_token_id(market): _snapshot(market=market)}.get,
        )
        requested_amount_usdc = Decimal("20")
        requested_size_shares = requested_amount_usdc / Decimal("0.60")
        executor = _ScriptedExecutor(
            _buy_result(
                market=market,
                requested_amount_usdc=requested_amount_usdc,
                status=OrderResultStatus.PARTIAL_FILL,
                matched_shares=Decimal("4"),
                remaining_shares=requested_size_shares - Decimal("4"),
                spent_usdc=Decimal("2.4"),
                reason="partial_fill",
            ),
            _sell_result(market=market, size_shares=Decimal("4")),
        )
        trading_decision_worker = TradingDecisionWorker(
            trading_decision_service=trading_decision_service,
            trading_service=TradingService(executor=executor),
            account_state_store=account_state_store,
            portfolio_budget_usdc=Decimal("100"),
            available_usdc=Decimal("100"),
            max_order_usdc=Decimal("20"),
            max_market_usdc=Decimal("20"),
            max_total_usdc=Decimal("100"),
            balance_usdc=Decimal("100"),
            allowance_usdc=Decimal("100"),
            max_open_orders=10,
            order_retry_limit=2,
        )

        result = await trading_decision_worker.process_event(_entry_event(market, trace_id="trace-partial"))

        assert result is not None
        assert result.review is not None
        assert result.review.order_result is not None
        assert result.review.order_result.status == OrderResultStatus.PARTIAL_FILL
        assert len(result.follow_up_intents) == 1
        assert result.follow_up_intents[0].size_shares == Decimal("4")
        assert len(result.follow_up_reviews) == 1
        assert result.follow_up_reviews[0].order_result is not None
        assert result.follow_up_reviews[0].order_result.status == OrderResultStatus.FULL_FILL
        assert len(executor.intents) == 2
        assert executor.intents[0].side == OrderSide.BUY
        assert executor.intents[1].side == OrderSide.SELL
        assert executor.intents[1].size_shares == Decimal("4")
        position = account_state_store.snapshot().get_position(market.condition_id, _no_token_id(market))
        assert position is not None
        assert position.shares == Decimal("4")
        assert position.open_sell_shares == Decimal("0")
        assert executor.intents[0].amount_usdc == requested_amount_usdc

    asyncio.run(run())


def test_trading_decision_worker_tracks_live_follow_up_sell_in_hot_state() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        account_state_store = _ready_account_state_store()
        strategy = build_strategy()
        market = _market(condition_id="condition", token_id="no-token", market_slug="token")
        registry.upsert(market)
        trading_decision_service = TradingDecisionService(
            extension_hooks=strategy,
            registry=registry,
            orderbook_reader={_no_token_id(market): _snapshot(market=market)}.get,
        )
        requested_amount_usdc = Decimal("6")
        filled_shares = Decimal("10")
        executor = _ScriptedExecutor(
            _buy_result(
                market=market,
                requested_amount_usdc=requested_amount_usdc,
                status=OrderResultStatus.FULL_FILL,
                matched_shares=filled_shares,
                remaining_shares=Decimal("0"),
                spent_usdc=requested_amount_usdc,
                reason="full_fill",
            ),
            _sell_result(
                market=market,
                size_shares=filled_shares,
                status=OrderResultStatus.LIVE,
                matched_shares=Decimal("0"),
                remaining_shares=filled_shares,
            ),
        )
        trading_decision_worker = TradingDecisionWorker(
            trading_decision_service=trading_decision_service,
            trading_service=TradingService(executor=executor),
            account_state_store=account_state_store,
            portfolio_budget_usdc=Decimal("100"),
            available_usdc=Decimal("100"),
            max_order_usdc=Decimal("20"),
            max_market_usdc=Decimal("20"),
            max_total_usdc=Decimal("100"),
            balance_usdc=Decimal("100"),
            allowance_usdc=Decimal("100"),
            max_open_orders=10,
            order_retry_limit=2,
        )

        result = await trading_decision_worker.process_event(_entry_event(market, trace_id="trace-live-sell"))

        assert result is not None
        assert result.review is not None
        assert result.review.order_result is not None
        assert result.review.order_result.status == OrderResultStatus.FULL_FILL
        assert len(result.follow_up_intents) == 1
        assert len(result.follow_up_reviews) == 1
        assert result.follow_up_reviews[0].order_result is not None
        assert result.follow_up_reviews[0].order_result.status == OrderResultStatus.LIVE
        snapshot = account_state_store.snapshot()
        position = snapshot.get_position(market.condition_id, _no_token_id(market))
        assert position is not None
        assert position.shares == filled_shares
        assert position.open_sell_shares == filled_shares
        open_sell_orders = snapshot.open_sell_orders_for_market(market.condition_id, _no_token_id(market))
        assert len(open_sell_orders) == 1
        assert open_sell_orders[0].status == OrderStatus.LIVE
        assert open_sell_orders[0].remaining_shares == filled_shares

    asyncio.run(run())


def test_trading_decision_worker_no_fill_releases_budget_for_next_market() -> None:
    async def run() -> None:
        registry = MarketRegistry()
        account_state_store = _ready_account_state_store()
        strategy = build_strategy()
        first = _market(condition_id="condition-1", token_id="no-1", market_slug="token-1")
        second = _market(condition_id="condition-2", token_id="no-2", market_slug="token-2")
        registry.upsert(first)
        registry.upsert(second)
        trading_decision_service = TradingDecisionService(
            extension_hooks=strategy,
            registry=registry,
            orderbook_reader={
                _no_token_id(first): _snapshot(market=first),
                _no_token_id(second): _snapshot(market=second),
            }.get,
        )
        requested_amount_usdc = Decimal("50")
        requested_size_shares = requested_amount_usdc / Decimal("0.60")
        executor = _ScriptedExecutor(
            _buy_result(
                market=first,
                requested_amount_usdc=requested_amount_usdc,
                status=OrderResultStatus.NO_FILL,
                matched_shares=Decimal("0"),
                remaining_shares=requested_size_shares,
                spent_usdc=Decimal("0"),
                reason="no_fill",
            ),
            _buy_result(
                market=second,
                requested_amount_usdc=requested_amount_usdc,
                status=OrderResultStatus.NO_FILL,
                matched_shares=Decimal("0"),
                remaining_shares=requested_size_shares,
                spent_usdc=Decimal("0"),
                reason="no_fill",
            ),
        )
        trading_decision_worker = TradingDecisionWorker(
            trading_decision_service=trading_decision_service,
            trading_service=TradingService(executor=executor),
            account_state_store=account_state_store,
            portfolio_budget_usdc=Decimal("100"),
            available_usdc=Decimal("100"),
            max_order_usdc=Decimal("50"),
            max_market_usdc=Decimal("50"),
            max_total_usdc=Decimal("100"),
            balance_usdc=Decimal("100"),
            allowance_usdc=Decimal("100"),
            max_open_orders=10,
            order_retry_limit=2,
        )

        first_result = await trading_decision_worker.process_event(
            _entry_event(first, trace_id="trace-no-fill-1")
        )
        assert first_result.review is not None
        assert first_result.review.order_result is not None
        assert first_result.review.order_result.status == OrderResultStatus.NO_FILL
        assert first_result.review.order_result.requested_amount_usdc == requested_amount_usdc
        assert first_result.review.order_result.spent_usdc == Decimal("0")
        assert any(
            event.event_type == DomainEventType.ORDER_NO_FILL for event in first_result.emitted_events
        )
        assert account_state_store.snapshot().allow_new_entries is True
        assert account_state_store.snapshot().balance_usdc == Decimal("100")

        second_result = await trading_decision_worker.process_event(
            _entry_event(second, trace_id="trace-no-fill-2")
        )

        assert second_result.plan is not None
        assert second_result.plan.ready_to_trade
        assert second_result.plan.allocation is not None
        assert second_result.plan.allocation.buy_budget_usdc == Decimal("50")
        assert second_result.review is not None
        assert second_result.review.order_result is not None
        assert second_result.review.order_result.status == OrderResultStatus.NO_FILL
        assert len(executor.intents) == 2
        assert executor.intents[0].amount_usdc == requested_amount_usdc
        assert executor.intents[1].amount_usdc == requested_amount_usdc

    asyncio.run(run())
