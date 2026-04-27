from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import OrderSide, OrderStatus, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.order import Order
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.trading_decision_worker import TradingDecisionWorker
from polymarket_trader.extension_api import ExtensionContext

from strategies.current.config import CurrentStrategyConfig, sports_tail_policy_from_config
from strategies.current.outcomes import describe_sports_market
from strategies.current.recovery import decide_recovery
from strategies.current.sports_tail import ExecutionPermission, SportsMarketType
from strategies.current.strategy import CurrentStrategy
from strategies.current.trading import decide_entry
from strategies.current.universe import select_market


def test_config_expresses_complete_sports_tail_policy() -> None:
    policy = sports_tail_policy_from_config(CurrentStrategyConfig())

    assert policy.enabled_market_types == (
        SportsMarketType.TOTALS,
        SportsMarketType.MONEYLINE,
        SportsMarketType.SPREADS,
    )
    assert policy.totals_execution_permission == ExecutionPermission.AUTO_EXECUTE
    assert policy.moneyline_execution_permission == ExecutionPermission.MANUAL_CONFIRM
    assert policy.spreads_execution_permission == ExecutionPermission.ALERT_ONLY


def test_universe_accepts_totals_moneyline_and_spreads_with_shared_descriptor_shape() -> None:
    config = CurrentStrategyConfig()
    markets = (
        _totals_market(),
        _moneyline_market(),
        _spreads_market(),
    )

    descriptors = tuple(describe_sports_market(market) for market in markets)
    decisions = tuple(select_market(config, market) for market in markets)

    assert [descriptor.accepted for descriptor in descriptors] == [True, True, True]
    assert [descriptor.market_type for descriptor in descriptors] == [
        SportsMarketType.TOTALS,
        SportsMarketType.MONEYLINE,
        SportsMarketType.SPREADS,
    ]
    assert [decision.selected for decision in decisions] == [True, True, True]


def test_sports_market_line_parser_handles_slug_decimal_without_using_event_date() -> None:
    market = Market(
        condition_id="slug-line-condition",
        market_slug="nhl-tb-mon-2026-04-26-total-4-5",
        market_question="TB vs MON total",
        event_title="TB vs MON 2026-04-26",
        category="Sports",
        tags=("NHL",),
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    descriptor = describe_sports_market(market)

    assert descriptor.accepted is True
    assert descriptor.line == Decimal("4.5")


def test_entry_rejects_sports_market_without_live_game_state_before_creating_buy() -> None:
    market = _totals_market()
    orderbook = _orderbook(token_id="over", best_ask=Decimal("0.98"))

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-1",
            market=market,
            token_id="over",
            orderbook=orderbook,
            amount_usdc=Decimal("10"),
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "missing_live_game_state"


def test_totals_over_locked_can_create_buy_only_after_full_sports_gate_passes() -> None:
    market = _totals_market()
    orderbook = _orderbook(token_id="over", best_ask=Decimal("0.98"))

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-2",
            market=market,
            token_id="over",
            orderbook=orderbook,
            amount_usdc=Decimal("10"),
            now=datetime(2026, 4, 27, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "NHL",
                    "home_name": "TB",
                    "away_name": "MON",
                    "home_score": 3,
                    "away_score": 2,
                    "period": "P3",
                    "seconds_remaining": 420,
                    "status": "live",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.token_id == "over"
    assert decision.price == Decimal("0.99")
    assert decision.amount_usdc == Decimal("10")
    assert decision.metadata["sports_tail_reason"] == "totals_over_locked"
    assert decision.metadata["sports_execution_permission"] == "auto_execute"


def test_entry_plan_preserves_event_metadata_through_application_entry_path() -> None:
    market = _totals_market()
    orderbook = _orderbook(token_id="over", best_ask=Decimal("0.98"))
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
    )

    plan = service.build_entry_plan(
        market=market,
        orderbook=orderbook,
        token_id="over",
        trace_id="trace-plan",
        portfolio_budget_usdc=Decimal("10"),
        available_usdc=Decimal("10"),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("10"),
        max_total_usdc=Decimal("10"),
        metadata={
            "source": "worker_payload",
            "sports_tail_game": {
                "league": "NHL",
                "home_name": "TB",
                "away_name": "MON",
                "home_score": 3,
                "away_score": 2,
                "period": "P3",
                "seconds_remaining": 420,
                "status": "live",
                "observed_at": "2026-04-27T00:00:00+00:00",
            },
        },
    )

    assert plan.ready_to_trade is True
    assert plan.intent is not None
    assert plan.intent.token_id == "over"
    assert plan.intent.price == Decimal("0.99")
    assert plan.metadata is not None
    assert plan.metadata["source"] == "worker_payload"
    assert plan.metadata["sports_tail_reason"] == "totals_over_locked"
    assert plan.metadata["sports_execution_permission"] == "auto_execute"


def test_entry_plan_zeroes_budget_when_sports_permission_is_not_auto_execute() -> None:
    market = _moneyline_market()
    orderbook = _orderbook(token_id="home", best_ask=Decimal("0.96"))
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
    )

    plan = service.build_entry_plan(
        market=market,
        orderbook=orderbook,
        token_id="home",
        trace_id="trace-manual-plan",
        portfolio_budget_usdc=Decimal("10"),
        available_usdc=Decimal("10"),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("10"),
        max_total_usdc=Decimal("10"),
        metadata={
            "sports_tail_game": {
                "league": "NBA",
                "home_name": "NYK",
                "away_name": "BOS",
                "home_score": 102,
                "away_score": 94,
                "period": "Q4",
                "seconds_remaining": 90,
                "status": "live",
                "observed_at": "2026-04-27T00:00:00+00:00",
            },
        },
    )

    assert plan.ready_to_trade is False
    assert plan.reason == "sports_tail_manual_confirm"
    assert plan.allocation is not None
    assert plan.allocation.buy_budget_usdc == Decimal("0")
    assert plan.metadata is not None
    assert plan.metadata["sports_tail_reason"] == "moneyline_late_lead"
    assert plan.metadata["sports_execution_permission"] == "manual_confirm"


def test_worker_publishes_skipped_plan_metadata_for_candidate_replay() -> None:
    result = asyncio.run(_run_worker_without_live_game_state())

    assert result is not None
    assert result.review is None
    assert result.plan is not None
    assert result.plan.reason == "missing_live_game_state"
    assert result.plan.allocation is not None
    assert result.plan.allocation.buy_budget_usdc == Decimal("0")
    assert result.emitted_event is not None
    assert result.emitted_event.event_type == DomainEventType.SKIPPED
    assert result.emitted_event.reason == "missing_live_game_state"
    assert result.emitted_event.payload["plan_metadata"]["source"] == "unit_test"
    assert result.emitted_event.payload["plan_metadata"]["provider_marker"] == "from_provider"
    assert result.emitted_event.payload["plan_metadata"]["sports_tail_reason"] == "missing_live_game_state"
    assert result.emitted_event.payload["plan_metadata"]["sports_tail_action"] == "reject"


def test_moneyline_manual_permission_keeps_candidate_out_of_auto_buy_path() -> None:
    market = _moneyline_market()
    orderbook = _orderbook(token_id="home", best_ask=Decimal("0.96"))

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-3",
            market=market,
            token_id="home",
            orderbook=orderbook,
            amount_usdc=Decimal("10"),
            now=datetime(2026, 4, 27, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "NBA",
                    "home_name": "NYK",
                    "away_name": "BOS",
                    "home_score": 102,
                    "away_score": 94,
                    "period": "Q4",
                    "seconds_remaining": 90,
                    "status": "live",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "sports_tail_manual_confirm"
    assert decision.metadata["sports_execution_permission"] == "manual_confirm"


def test_recovery_manages_all_sports_target_tokens_instead_of_fixed_primary_token() -> None:
    market = _moneyline_market()
    position = Position(
        condition_id=market.condition_id,
        token_id="away",
        shares=Decimal("4"),
        cost_usdc=Decimal("3.5"),
        market_slug=market.market_slug,
    )
    open_buy = Order(
        condition_id=market.condition_id,
        token_id="home",
        side=OrderSide.BUY,
        order_type=OrderType.FAK,
        price=Decimal("0.96"),
        status=OrderStatus.LIVE,
        order_id="buy-1",
        market_slug=market.market_slug,
    )

    decision = decide_recovery(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-4",
            market=market,
            position=position,
            open_orders=(open_buy,),
        ),
    )

    assert decision.reason == "strategy_recovery"
    assert [(action.action.value, action.token_id) for action in decision.actions] == [
        ("cancel", "home"),
        ("sell", "away"),
    ]


def _totals_market() -> Market:
    return Market(
        condition_id="totals-condition",
        market_slug="nhl-tb-mon-total-4-5",
        market_question="TB vs MON total over/under 4.5",
        event_title="TB vs MON",
        category="Sports",
        tags=("NHL",),
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _moneyline_market() -> Market:
    return Market(
        condition_id="moneyline-condition",
        market_slug="nba-nyk-bos-moneyline",
        market_question="NYK vs BOS moneyline",
        event_title="NYK vs BOS",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="home", outcome="NYK"),
            MarketOutcome(token_id="away", outcome="BOS"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _spreads_market() -> Market:
    return Market(
        condition_id="spreads-condition",
        market_slug="nba-nyk-bos-spread-minus-3-5",
        market_question="NYK vs BOS spread -3.5",
        event_title="NYK vs BOS",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="home", outcome="NYK -3.5"),
            MarketOutcome(token_id="away", outcome="BOS +3.5"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _orderbook(token_id: str, best_ask: Decimal) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id=token_id,
        best_bid=best_ask - Decimal("0.01"),
        best_ask=best_ask,
        bids=(PriceLevel(price=best_ask - Decimal("0.01"), size=Decimal("20")),),
        asks=(PriceLevel(price=best_ask, size=Decimal("20")),),
        received_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        condition_id="condition",
    )


async def _run_worker_without_live_game_state():
    market = _totals_market()
    orderbook = _orderbook(token_id="over", best_ask=Decimal("0.98"))
    registry = MarketRegistry()
    registry.upsert(market)
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
        registry=registry,
        orderbook_reader=lambda token_id: orderbook if token_id == "over" else None,
    )
    worker = TradingDecisionWorker(
        trading_decision_service=service,
        portfolio_budget_usdc=Decimal("10"),
        available_usdc=Decimal("10"),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("10"),
        max_total_usdc=Decimal("10"),
        entry_metadata_provider=lambda event, snapshot: {"provider_marker": "from_provider"},
    )
    return await worker.process_event(
        DomainEvent(
            trace_id="trace-worker-skip",
            event_type=DomainEventType.ORDERBOOK_SNAPSHOT_UPDATED,
            event_id="event-worker-skip",
            market_slug=market.market_slug,
            condition_id=market.condition_id,
            token_id="over",
            reason="new_market",
            created_at=orderbook.received_at,
            payload={"source": "unit_test"},
        )
    )
