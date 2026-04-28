from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import DomainEvent, DomainEventType, Fill
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import OrderResult, OrderResultStatus, OrderSide, OrderStatus, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.order import Order
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.trading_decision_worker import TradingDecisionWorker
from polymarket_trader.extension_api import ExtensionContext

from strategies.current.config import CurrentStrategyConfig, sports_tail_policy_from_config
from strategies.current.outcomes import SportsMarketFamily, describe_sports_market
from strategies.current.recovery import decide_recovery
from strategies.current.sports_tail import (
    ExecutionPermission,
    LiveGameStatus,
    SportsMarketType,
    live_game_state_from_metadata,
)
from strategies.current.strategy import CurrentStrategy
from strategies.current.trading import decide_entry
from strategies.current.universe import select_market


def _manual_moneyline_config() -> CurrentStrategyConfig:
    return CurrentStrategyConfig(
        sports_moneyline_execution_permission=ExecutionPermission.MANUAL_CONFIRM,
    )


def test_config_expresses_complete_sports_tail_policy() -> None:
    policy = sports_tail_policy_from_config(CurrentStrategyConfig())

    assert policy.enabled_market_types == (
        SportsMarketType.TOTALS,
        SportsMarketType.MONEYLINE,
        SportsMarketType.SPREADS,
    )
    assert policy.totals_execution_permission == ExecutionPermission.AUTO_EXECUTE
    assert policy.moneyline_execution_permission == ExecutionPermission.AUTO_EXECUTE
    assert policy.spreads_execution_permission == ExecutionPermission.AUTO_EXECUTE


def test_live_game_metadata_preserves_scheduled_status() -> None:
    game = live_game_state_from_metadata(
        {
            "sports_tail_game": {
                "league": "NBA",
                "home_name": "Suns",
                "away_name": "Thunder",
                "home_score": 0,
                "away_score": 0,
                "period": "STATUS_SCHEDULED",
                "seconds_remaining": None,
                "status": "scheduled",
                "observed_at": "2026-04-27T09:00:00+00:00",
            }
        }
    )

    assert game is not None
    assert game.status == LiveGameStatus.SCHEDULED


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


def test_universe_accepts_tennis_single_game_market_for_live_tail_scope() -> None:
    decision = select_market(CurrentStrategyConfig(), _tennis_totals_market())
    descriptor = describe_sports_market(_tennis_totals_market())

    assert decision.selected is True
    assert descriptor.market_family == SportsMarketFamily.SINGLE_GAME
    assert descriptor.line == Decimal("21.5")


def test_universe_accepts_tennis_market_when_gamma_tags_are_missing_but_slug_has_league() -> None:
    market = Market(
        condition_id="tagless-tennis-condition",
        market_slug="atp-donald-mejia-2026-04-28",
        market_question="Matthew William Donald vs Nicolas Mejia",
        event_title="Mauthausen: Matthew William Donald vs Nicolas Mejia",
        event_slug="atp-donald-mejia-2026-04-28",
        category=None,
        tags=(),
        outcomes=(
            MarketOutcome(token_id="donald", outcome="Matthew William Donald"),
            MarketOutcome(token_id="mejia", outcome="Nicolas Mejia"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = select_market(CurrentStrategyConfig(), market)

    assert decision.selected is True
    assert decision.reason == "sports_market_selected"


def test_market_descriptor_separates_single_game_from_series_outright_and_esports() -> None:
    single_game = _moneyline_market()
    series_winner = _series_winner_market()
    series_totals = _series_totals_market()
    outright = _outright_market()
    esports = _esports_series_market()

    descriptors = {
        "single_game": describe_sports_market(single_game),
        "series_winner": describe_sports_market(series_winner),
        "series_totals": describe_sports_market(series_totals),
        "outright": describe_sports_market(outright),
        "esports": describe_sports_market(esports),
    }

    assert descriptors["single_game"].market_family == SportsMarketFamily.SINGLE_GAME
    assert descriptors["series_winner"].market_family == SportsMarketFamily.SERIES
    assert descriptors["series_totals"].market_family == SportsMarketFamily.SERIES
    assert descriptors["outright"].market_family == SportsMarketFamily.OUTRIGHT
    assert descriptors["esports"].market_family == SportsMarketFamily.ESPORTS


def test_universe_excludes_non_single_game_markets_from_auto_strategy_scope() -> None:
    config = CurrentStrategyConfig()

    decisions = {
        "series_winner": select_market(config, _series_winner_market()),
        "series_totals": select_market(config, _series_totals_market()),
        "outright": select_market(config, _outright_market()),
        "esports": select_market(config, _esports_series_market()),
    }

    assert decisions["series_winner"].selected is False
    assert decisions["series_winner"].reason == "series_market_not_auto_tradable"
    assert decisions["series_totals"].selected is False
    assert decisions["series_totals"].reason == "series_market_not_auto_tradable"
    assert decisions["outright"].selected is False
    assert decisions["outright"].reason == "outright_market_not_auto_tradable"
    assert decisions["esports"].selected is False
    assert decisions["esports"].reason == "esports_market_not_auto_tradable"


def test_entry_rejects_series_market_before_single_game_live_score_can_create_buy() -> None:
    market = _series_winner_market()
    orderbook = _orderbook(token_id="series-home", best_ask=Decimal("0.45"))

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-series",
            market=market,
            token_id="series-home",
            orderbook=orderbook,
            amount_usdc=Decimal("10"),
            metadata={"sports_tail_game": _moneyline_live_game()},
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "series_market_not_auto_tradable"
    assert decision.metadata["sports_market_family"] == "series"


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


def test_sports_market_line_parser_handles_pt_decimal_without_truncating_integer() -> None:
    market = Market(
        condition_id="slug-pt-line-condition",
        market_slug="nba-atl-nyk-2026-04-28-total-214pt5",
        market_question="Atlanta Hawks vs New York Knicks total",
        event_title="Atlanta Hawks vs New York Knicks 2026-04-28",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    descriptor = describe_sports_market(market)

    assert descriptor.accepted is True
    assert descriptor.line == Decimal("214.5")


def test_sports_market_line_parser_handles_total_games_pt_decimal_without_using_event_date() -> None:
    descriptor = describe_sports_market(_esports_series_market())

    assert descriptor.accepted is True
    assert descriptor.market_family == SportsMarketFamily.ESPORTS
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
    assert decision.price == Decimal("0.98")
    assert decision.amount_usdc == Decimal("10")
    assert decision.metadata["sports_tail_reason"] == "totals_over_locked"
    assert decision.metadata["sports_execution_permission"] == "auto_execute"


def test_entry_plan_preserves_event_metadata_through_application_entry_path() -> None:
    market = _totals_market()
    orderbook = _orderbook(token_id="over", best_ask=Decimal("0.98"))
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
        orderbook_reader=lambda token_id: _orderbook(token_id=token_id, best_ask=Decimal("0.02"))
        if token_id == "under"
        else None,
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
    assert plan.intent.price == Decimal("0.98")
    assert plan.metadata is not None
    assert plan.metadata["source"] == "worker_payload"
    assert plan.metadata["sports_tail_reason"] == "totals_over_locked"
    assert plan.metadata["sports_execution_permission"] == "auto_execute"


def test_follow_up_exit_price_aligns_to_market_tick_size() -> None:
    market = _totals_market().with_tick_size(Decimal("0.01"))
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    decisions = strategy.decide_follow_up(
        ExtensionContext(
            trace_id="trace-follow-up-tick",
            market=market,
            token_id="over",
            order_result=OrderResult(
                trace_id="trace-follow-up-tick",
                condition_id=market.condition_id,
                token_id="over",
                market_slug=market.market_slug,
                status=OrderResultStatus.FULL_FILL,
                side=OrderSide.BUY,
                price=Decimal("0.99"),
                requested_amount_usdc=Decimal("3"),
                matched_shares=Decimal("3"),
                spent_usdc=Decimal("3"),
                reason="virtual_fill",
            ),
        )
    )

    assert len(decisions) == 1
    assert decisions[0].price == Decimal("0.99")
    assert decisions[0].metadata["sports_exit_target_price"] == "0.99"


def test_entry_plan_zeroes_budget_when_sports_permission_is_not_auto_execute() -> None:
    market = _moneyline_market()
    orderbook = _orderbook(token_id="home", best_ask=Decimal("0.96"))
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(config=_manual_moneyline_config()).hooks,
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


def test_tennis_allocation_filters_opposite_side_before_equal_weight_budget() -> None:
    market = _tennis_moneyline_market()
    registry = MarketRegistry()
    registry.upsert(market)
    orderbooks = {
        "tennis-home": _orderbook(token_id="tennis-home", best_ask=Decimal("0.24")),
        "tennis-away": _orderbook(token_id="tennis-away", best_ask=Decimal("0.77")),
    }
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
        registry=registry,
        orderbook_reader=lambda token_id: orderbooks.get(token_id),
    )

    plan = service.build_entry_plan(
        market=market,
        orderbook=orderbooks["tennis-away"],
        token_id="tennis-away",
        trace_id="trace-tennis-allocation",
        portfolio_budget_usdc=Decimal("5"),
        available_usdc=Decimal("5"),
        max_order_usdc=Decimal("5"),
        max_market_usdc=Decimal("5"),
        max_total_usdc=Decimal("5"),
        metadata={
            "sports_tail_game": {
                "league": "ATP Challenger",
                "home_name": "Amir Omarkhanov",
                "away_name": "Denis Yevseyev",
                "home_score": 0,
                "away_score": 0,
                "period": "S1",
                "status": "live",
                "observed_at": "2026-04-27T00:00:00+00:00",
                "tennis_state": {
                    "home_sets_won": 0,
                    "away_sets_won": 0,
                    "current_set": 1,
                    "home_current_set_games": 3,
                    "away_current_set_games": 5,
                    "home_total_games": 3,
                    "away_total_games": 5,
                },
            },
        },
    )

    assert plan.ready_to_trade is True
    assert plan.allocation is not None
    assert plan.allocation.buy_budget_usdc == Decimal("5")
    assert plan.intent is not None
    assert plan.intent.token_id == "tennis-away"
    assert plan.intent.amount_usdc == Decimal("5")


def test_entry_plan_creates_intent_after_manual_confirmation_metadata() -> None:
    market = _moneyline_market()
    orderbook = _orderbook(token_id="home", best_ask=Decimal("0.96"))
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(config=_manual_moneyline_config()).hooks,
    )

    plan = service.build_entry_plan(
        market=market,
        orderbook=orderbook,
        token_id="home",
        trace_id="trace-manual-confirmed",
        portfolio_budget_usdc=Decimal("10"),
        available_usdc=Decimal("10"),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("10"),
        max_total_usdc=Decimal("10"),
        metadata={
            "sports_tail_manual_confirmed": True,
            "sports_tail_confirmed_by": "operator-1",
            "sports_tail_confirm_reason": "score_verified",
            "sports_tail_game": _moneyline_live_game(),
        },
    )

    assert plan.ready_to_trade is True
    assert plan.intent is not None
    assert plan.intent.token_id == "home"
    assert plan.metadata is not None
    assert plan.metadata["sports_tail_manual_confirmed"] is True
    assert plan.metadata["sports_tail_confirmed_by"] == "operator-1"
    assert plan.metadata["sports_tail_confirm_reason"] == "score_verified"


def test_strategy_risk_blocks_event_exposure_before_buy_intent() -> None:
    market = _totals_market()
    orderbook = _orderbook(token_id="over", best_ask=Decimal("0.98"))
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
        orderbook_reader=lambda token_id: _orderbook(token_id=token_id, best_ask=Decimal("0.02"))
        if token_id == "under"
        else None,
    )

    plan = service.build_entry_plan(
        market=market,
        orderbook=orderbook,
        token_id="over",
        trace_id="trace-event-risk",
        portfolio_budget_usdc=Decimal("10"),
        available_usdc=Decimal("10"),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("100"),
        max_total_usdc=Decimal("100"),
        positions=(
            Position(
                condition_id=market.condition_id,
                token_id="under",
                shares=Decimal("24"),
                cost_usdc=Decimal("24"),
                market_slug=market.market_slug,
            ),
        ),
        metadata={"sports_tail_game": _totals_live_game()},
    )

    assert plan.intent is None
    assert plan.allocation is not None
    assert plan.allocation.buy_budget_usdc == Decimal("0")
    assert plan.allocation.reason == "sports_event_exposure_limit"
    assert plan.metadata is not None
    assert plan.metadata["sports_risk_reason"] == "sports_event_exposure_limit"


def test_strategy_risk_uses_account_fills_for_daily_entry_limit() -> None:
    market = _totals_market()
    orderbook = _orderbook(token_id="over", best_ask=Decimal("0.98"))
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(
            config=CurrentStrategyConfig(sports_max_daily_entry_usdc=Decimal("15"))
        ).hooks,
    )

    plan = service.build_entry_plan(
        market=market,
        orderbook=orderbook,
        account_snapshot=AccountSnapshot(
            balance_usdc=Decimal("100"),
            allowance_usdc=Decimal("100"),
            allow_new_entries=True,
            fills=(
                Fill(
                    trace_id="old-buy",
                    condition_id=market.condition_id,
                    token_id="over",
                    side="BUY",
                    notional_usdc=Decimal("12"),
                    confirmed_at=datetime(2026, 4, 27, 1, tzinfo=timezone.utc),
                ),
            ),
        ),
        token_id="over",
        trace_id="trace-daily-risk",
        portfolio_budget_usdc=Decimal("10"),
        available_usdc=Decimal("100"),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("100"),
        max_total_usdc=Decimal("100"),
        metadata={"sports_tail_game": _totals_live_game()},
    )

    assert plan.intent is None
    assert plan.allocation is not None
    assert plan.allocation.buy_budget_usdc == Decimal("0")
    assert plan.allocation.reason == "sports_daily_entry_limit"
    assert plan.metadata is not None
    assert plan.metadata["sports_daily_entry_usdc"] == "12"


def test_strategy_risk_blocks_consecutive_loss_pause() -> None:
    market = _totals_market()
    orderbook = _orderbook(token_id="over", best_ask=Decimal("0.98"))
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
    )

    plan = service.build_entry_plan(
        market=market,
        orderbook=orderbook,
        token_id="over",
        trace_id="trace-loss-risk",
        portfolio_budget_usdc=Decimal("10"),
        available_usdc=Decimal("10"),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("100"),
        max_total_usdc=Decimal("100"),
        metadata={
            "sports_tail_consecutive_losses": 3,
            "sports_tail_game": _totals_live_game(),
        },
    )

    assert plan.intent is None
    assert plan.allocation is not None
    assert plan.allocation.reason == "sports_consecutive_loss_pause"
    assert plan.metadata is not None
    assert plan.metadata["sports_consecutive_losses"] == 3


def test_entry_plan_does_not_reenter_market_with_existing_position_and_exit_order() -> None:
    market = _tennis_moneyline_market()
    position = Position(
        condition_id=market.condition_id,
        token_id="tennis-home",
        shares=Decimal("8"),
        cost_usdc=Decimal("4.5"),
        market_slug=market.market_slug,
        open_sell_shares=Decimal("8"),
    )
    exit_order = Order(
        trace_id="trace-exit",
        condition_id=market.condition_id,
        token_id="tennis-home",
        market_slug=market.market_slug,
        side=OrderSide.SELL,
        order_type=OrderType.GTC,
        price=Decimal("0.99"),
        size_shares=Decimal("8"),
        remaining_shares=Decimal("8"),
        status=OrderStatus.LIVE,
        order_id="exit-order",
    )
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
    )

    plan = service.build_entry_plan(
        market=market,
        orderbook=_orderbook(token_id="tennis-home", best_ask=Decimal("0.56")),
        token_id="tennis-home",
        trace_id="trace-reentry",
        portfolio_budget_usdc=Decimal("10"),
        available_usdc=Decimal("10"),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("10"),
        max_total_usdc=Decimal("20"),
        positions=(position,),
        open_orders=(exit_order,),
        metadata={"sports_tail_game": _tennis_near_locked_live_game()},
    )

    assert plan.intent is None
    assert plan.allocation is not None
    assert plan.allocation.reason == "open_exit_detected"
    assert plan.metadata is not None
    assert plan.metadata["sports_tail_reason"] == "open_exit_detected"


def test_admin_live_state_store_projects_manual_candidate_and_confirmation() -> None:
    result = asyncio.run(_run_admin_manual_candidate_flow())

    candidates = result["candidates"]
    confirmation = result["confirmation"]
    live_states = result["live_states"]

    assert live_states["total"] == 1
    assert candidates["total"] == 1
    candidate = candidates["items"][0]
    assert candidate["condition_id"] == "moneyline-condition"
    assert candidate["token_id"] == "home"
    assert candidate["action"] == "manual_confirm"
    assert candidate["execution_permission"] == "manual_confirm"
    assert candidate["confirmable"] is True

    assert confirmation["status"] == "ok"
    assert confirmation["candidate"]["ready_to_trade"] is True
    assert confirmation["candidate"]["confirmable"] is False
    assert confirmation["candidate"]["intent"]["token_id"] == "home"
    assert confirmation["review"]["submitted"] is True
    assert confirmation["review"]["risk_decision"]["passed"] is True
    assert confirmation["review"]["order_result"]["status"] == "no_fill"


def test_admin_candidates_include_rejects_and_filter_by_action_permission_status() -> None:
    result = asyncio.run(_run_admin_candidate_filter_flow())

    assert result["all"]["total"] == 1
    rejected = result["all"]["items"][0]
    assert rejected["action"] == "reject"
    assert rejected["accepted"] is False
    assert rejected["reason"] == "outcome_not_locked"
    assert result["rejects"]["total"] == 1
    assert result["live_totals"]["total"] == 1
    assert result["manual"]["total"] == 0


def test_admin_candidates_use_runtime_metadata_source_not_full_registry() -> None:
    result = asyncio.run(_run_admin_candidate_metadata_source_flow())

    assert len(result["items"]) == 2
    assert result["total"] == 2
    assert result["has_more"] is False
    assert result["source_markets"] == 2


def test_admin_confirmation_refuses_non_confirmable_candidate() -> None:
    result = asyncio.run(_run_admin_auto_candidate_confirmation_attempt())

    assert result["candidates"]["total"] == 1
    candidate = result["candidates"]["items"][0]
    assert candidate["action"] == "auto_execute"
    assert candidate["confirmable"] is False
    assert result["confirmation"]["status"] == "failed"
    assert result["confirmation"]["reason"] == "candidate_not_confirmable"
    assert result["confirmation"]["candidate"]["confirmable"] is False


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


def test_worker_treats_live_state_entry_signal_as_entry_replay_trigger() -> None:
    result = asyncio.run(_run_worker_with_live_state_entry_signal())

    assert result is not None
    assert result.plan is not None
    assert result.plan.ready_to_trade is True
    assert result.plan.intent is not None
    assert result.plan.intent.token_id == "over"
    assert result.plan.metadata["sports_tail_reason"] == "totals_over_locked"


def test_moneyline_default_permission_enters_auto_buy_path() -> None:
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

    assert decision.action.value == "buy"
    assert decision.token_id == "home"
    assert decision.price == Decimal("0.96")
    assert decision.metadata["sports_execution_permission"] == "auto_execute"


def test_spreads_default_permission_enters_auto_buy_path() -> None:
    market = _spreads_market()
    orderbook = _orderbook(token_id="home", best_ask=Decimal("0.95"))

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-spread-alert",
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
                    "home_score": 106,
                    "away_score": 98,
                    "period": "Q4",
                    "seconds_remaining": 60,
                    "status": "live",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.token_id == "home"
    assert decision.price == Decimal("0.95")
    assert decision.metadata["sports_tail_reason"] == "spreads_late_cover"
    assert decision.metadata["sports_execution_permission"] == "auto_execute"


def test_follow_up_sell_carries_explicit_exit_plan_metadata() -> None:
    market = _totals_market()
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    decisions = strategy.decide_follow_up(
        ExtensionContext(
            trace_id="trace-follow-up",
            market=market,
            order_result=OrderResult(
                trace_id="trace-follow-up",
                condition_id=market.condition_id,
                token_id="over",
                status=OrderResultStatus.FULL_FILL,
                market_slug=market.market_slug,
                side=OrderSide.BUY,
                matched_shares=Decimal("3"),
            ),
        )
    )

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.action.value == "sell"
    assert decision.metadata["sports_exit_plan_version"] == "1"
    assert decision.metadata["sports_exit_plan"]["primary_action"] == "place_follow_up_gtc_sell_after_buy_fill"
    assert decision.metadata["sports_exit_plan"]["target_size_shares"] == "3"


def test_recovery_pauses_new_entries_when_live_state_is_abnormal() -> None:
    market = _totals_market()

    decision = decide_recovery(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-abnormal-recovery",
            market=market,
            now=datetime(2026, 4, 27, 1, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "NHL",
                    "home_name": "TB",
                    "away_name": "MON",
                    "home_score": 3,
                    "away_score": 2,
                    "period": "P3",
                    "seconds_remaining": 0,
                    "status": "ended",
                    "observed_at": "2026-04-27T00:59:55+00:00",
                }
            },
        ),
    )

    assert decision.pause_trading is True
    assert decision.pause_reason == "sports_live_state_ended"


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


def _moneyline_market_for_index(index: int) -> Market:
    return Market(
        condition_id=f"moneyline-condition-{index}",
        market_slug=f"nba-nyk-bos-moneyline-{index}",
        market_question=f"NYK vs BOS moneyline {index}",
        event_title="NYK vs BOS",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id=f"home-{index}", outcome="NYK"),
            MarketOutcome(token_id=f"away-{index}", outcome="BOS"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _moneyline_live_game() -> dict[str, object]:
    return {
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


def _totals_live_game() -> dict[str, object]:
    return {
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


def _tennis_near_locked_live_game() -> dict[str, object]:
    return {
        "league": "ATP",
        "home_name": "Amir Omarkhanov",
        "away_name": "Denis Yevseyev",
        "home_score": 0,
        "away_score": 0,
        "period": "S1",
        "status": "live",
        "observed_at": "2026-04-27T00:00:00+00:00",
        "tennis_state": {
            "home_sets_won": 0,
            "away_sets_won": 0,
            "current_set": 1,
            "home_current_set_games": 5,
            "away_current_set_games": 3,
            "home_total_games": 5,
            "away_total_games": 3,
            "set_scores": ((5, 3),),
        },
    }


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


def _series_winner_market() -> Market:
    return Market(
        condition_id="series-winner-condition",
        market_slug="nba-playoffs-who-will-win-series-knicks-vs-hawks",
        market_question="NBA Playoffs: Who Will Win Series? - Knicks vs. Hawks",
        event_title="NBA Playoffs: Who Will Win Series? - Knicks vs. Hawks",
        category="Sports",
        tags=("NBA", "2026 NBA Playoffs", "Basketball"),
        outcomes=(
            MarketOutcome(token_id="series-home", outcome="Knicks"),
            MarketOutcome(token_id="series-away", outcome="Hawks"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _series_totals_market() -> Market:
    return Market(
        condition_id="series-total-condition",
        market_slug="nhl-playoffs-ducks-vs-oilers-total-games-ou-5pt5",
        market_question="NHL Playoffs: Ducks vs. Oilers Total Games O/U 5.5",
        event_title="NHL Playoffs: Ducks vs. Oilers Total Games O/U 5.5",
        category="Sports",
        tags=("NHL", "2026 NHL Playoffs", "Hockey"),
        outcomes=(
            MarketOutcome(token_id="series-over", outcome="Over 5.5"),
            MarketOutcome(token_id="series-under", outcome="Under 5.5"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _outright_market() -> Market:
    return Market(
        condition_id="outright-condition",
        market_slug="nhl-stanley-cup-winner-nation",
        market_question="NHL: Stanley Cup Winner USA or Canada?",
        event_title="NHL: Stanley Cup Winner USA or Canada?",
        category="Sports",
        tags=("NHL", "Stanley Cup", "Hockey"),
        outcomes=(
            MarketOutcome(token_id="usa", outcome="USA"),
            MarketOutcome(token_id="canada", outcome="Canada"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _esports_series_market() -> Market:
    return Market(
        condition_id="esports-condition",
        market_slug="hok-esp-ge-2026-04-27-total-games-4pt5",
        market_question="Honor of Kings: eStar Pro vs Geekay Esports total games 4.5",
        event_title="Honor of Kings: eStar Pro vs Geekay Esports (BO5)",
        category="Sports",
        tags=("Esports", "Honor of Kings", "Games", "Sports"),
        outcomes=(
            MarketOutcome(token_id="esports-over", outcome="Over"),
            MarketOutcome(token_id="esports-under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _tennis_totals_market() -> Market:
    return Market(
        condition_id="tennis-total-condition",
        market_slug="wta-zolotar-papamic-2026-04-27-total-21pt5",
        market_question="Rada Zolotareva vs Despina Papamichail total games 21.5",
        event_title="Rada Zolotareva vs Despina Papamichail",
        event_slug="wta-zolotar-papamic-2026-04-27",
        category="Sports",
        tags=("WTA", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="tennis-over", outcome="Over"),
            MarketOutcome(token_id="tennis-under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _tennis_moneyline_market() -> Market:
    return Market(
        condition_id="tennis-moneyline-condition",
        market_slug="atp-omarkha-yevseye-2026-04-27",
        market_question="Amir Omarkhanov vs Denis Yevseyev",
        event_title="Amir Omarkhanov vs Denis Yevseyev",
        event_slug="atp-omarkha-yevseye-2026-04-27",
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="tennis-home", outcome="Amir Omarkhanov"),
            MarketOutcome(token_id="tennis-away", outcome="Denis Yevseyev"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
        min_order_size=Decimal("5"),
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


class _MarketWs:
    def __init__(self, snapshots: dict[str, OrderbookSnapshot]) -> None:
        self._snapshots = snapshots

    def snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        return self._snapshots.get(token_id)


class _NoFillExecutor:
    async def submit(self, intent) -> OrderResult:
        return OrderResult(
            trace_id=intent.trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            status=OrderResultStatus.NO_FILL,
            intent=intent,
            market_slug=intent.market_slug,
            side=intent.side,
            order_type=intent.order_type,
            price=intent.price,
            requested_amount_usdc=intent.amount_usdc,
            reason="unit_test_no_fill",
        )


async def _run_admin_manual_candidate_flow() -> dict[str, object]:
    market = _moneyline_market()
    orderbook = _orderbook(token_id="home", best_ask=Decimal("0.96"))
    registry = MarketRegistry()
    registry.upsert(market)
    market_ws = _MarketWs({"home": orderbook})
    account_state = AccountStateStore()
    account_state.update_balances(balance_usdc=Decimal("10"), allowance_usdc=Decimal("10"))
    service = AdminService(
        runtime=SimpleNamespace(
            settings=SimpleNamespace(
                portfolio_budget_usdc=Decimal("10"),
                max_order_usdc=Decimal("10"),
                max_market_usdc=Decimal("10"),
                max_total_usdc=Decimal("10"),
                max_open_orders=10,
                order_retry_limit=2,
            ),
            registry=registry,
            market_ws_worker=market_ws,
            account_state_store=account_state,
            entry_metadata_store=EntryMetadataStore(),
            trading_decision_service=TradingDecisionService(
                extension_hooks=CurrentStrategy(config=_manual_moneyline_config()).hooks,
                registry=registry,
                orderbook_reader=market_ws.snapshot,
            ),
            trading_service=TradingService(executor=_NoFillExecutor()),
            event_bus=None,
        )
    )

    await service.upsert_sports_live_state(
        sports_tail_game=_moneyline_live_game(),
        condition_id=market.condition_id,
        source="unit_test",
    )
    return {
        "live_states": await service.list_sports_live_states(limit=10, offset=0),
        "candidates": await service.list_sports_tail_candidates(limit=10, offset=0),
        "confirmation": await service.confirm_sports_tail_candidate(
            condition_id=market.condition_id,
            token_id="home",
            operator="operator-1",
            note="score_verified",
        ),
    }


async def _run_admin_auto_candidate_confirmation_attempt() -> dict[str, object]:
    market = _totals_market()
    orderbook = _orderbook(token_id="over", best_ask=Decimal("0.98"))
    registry = MarketRegistry()
    registry.upsert(market)
    market_ws = _MarketWs({"over": orderbook})
    account_state = AccountStateStore()
    account_state.update_balances(balance_usdc=Decimal("10"), allowance_usdc=Decimal("10"))
    service = AdminService(
        runtime=SimpleNamespace(
            settings=SimpleNamespace(
                portfolio_budget_usdc=Decimal("10"),
                max_order_usdc=Decimal("10"),
                max_market_usdc=Decimal("10"),
                max_total_usdc=Decimal("10"),
                max_open_orders=10,
                order_retry_limit=2,
            ),
            registry=registry,
            market_ws_worker=market_ws,
            account_state_store=account_state,
            entry_metadata_store=EntryMetadataStore(),
            trading_decision_service=TradingDecisionService(
                extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
                registry=registry,
                orderbook_reader=market_ws.snapshot,
            ),
            trading_service=TradingService(executor=_NoFillExecutor()),
            event_bus=None,
        )
    )

    await service.upsert_sports_live_state(
        sports_tail_game={
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
        condition_id=market.condition_id,
        source="unit_test",
    )
    return {
        "candidates": await service.list_sports_tail_candidates(limit=10, offset=0),
        "confirmation": await service.confirm_sports_tail_candidate(
            condition_id=market.condition_id,
            token_id="over",
            operator="operator-1",
            note="should_not_submit",
        ),
    }


async def _run_admin_candidate_filter_flow() -> dict[str, object]:
    market = _totals_market()
    orderbook = _orderbook(token_id="under", best_ask=Decimal("0.98"))
    registry = MarketRegistry()
    registry.upsert(market)
    market_ws = _MarketWs({"under": orderbook})
    account_state = AccountStateStore()
    account_state.update_balances(balance_usdc=Decimal("10"), allowance_usdc=Decimal("10"))
    service = AdminService(
        runtime=SimpleNamespace(
            settings=SimpleNamespace(
                portfolio_budget_usdc=Decimal("10"),
                max_order_usdc=Decimal("10"),
                max_market_usdc=Decimal("10"),
                max_total_usdc=Decimal("10"),
                max_open_orders=10,
                order_retry_limit=2,
            ),
            registry=registry,
            market_ws_worker=market_ws,
            account_state_store=account_state,
            entry_metadata_store=EntryMetadataStore(),
            trading_decision_service=TradingDecisionService(
                extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
                registry=registry,
                orderbook_reader=market_ws.snapshot,
            ),
            trading_service=TradingService(executor=_NoFillExecutor()),
            event_bus=None,
        )
    )

    await service.upsert_sports_live_state(
        sports_tail_game={
            **_totals_live_game(),
            "home_score": 1,
            "away_score": 1,
            "seconds_remaining": 900,
        },
        condition_id=market.condition_id,
        source="unit_test",
    )
    return {
        "all": await service.list_sports_tail_candidates(limit=10, offset=0),
        "rejects": await service.list_sports_tail_candidates(limit=10, offset=0, action="reject", accepted=False),
        "live_totals": await service.list_sports_tail_candidates(
            limit=10,
            offset=0,
            market_type="totals",
            game_status="live",
            league="NHL",
        ),
        "manual": await service.list_sports_tail_candidates(
            limit=10,
            offset=0,
            execution_permission="manual_confirm",
        ),
    }


async def _run_admin_candidate_metadata_source_flow() -> dict[str, object]:
    registry = MarketRegistry()
    snapshots: dict[str, OrderbookSnapshot] = {}
    live_store = EntryMetadataStore()
    for index in range(5):
        market = _moneyline_market_for_index(index)
        registry.upsert(market)
        if index >= 2:
            continue
        snapshots[f"home-{index}"] = _orderbook(token_id=f"home-{index}", best_ask=Decimal("0.96"))
        live_store.upsert(
            condition_id=market.condition_id,
            source="unit_test",
            metadata={"sports_tail_game": _moneyline_live_game()},
        )

    market_ws = _MarketWs(snapshots)
    account_state = AccountStateStore()
    account_state.update_balances(balance_usdc=Decimal("10"), allowance_usdc=Decimal("10"))
    service = AdminService(
        runtime=SimpleNamespace(
            settings=SimpleNamespace(
                portfolio_budget_usdc=Decimal("10"),
                max_order_usdc=Decimal("10"),
                max_market_usdc=Decimal("10"),
                max_total_usdc=Decimal("10"),
                max_open_orders=10,
                order_retry_limit=2,
            ),
            registry=registry,
            market_ws_worker=market_ws,
            account_state_store=account_state,
            entry_metadata_store=live_store,
            trading_decision_service=TradingDecisionService(
                extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
                registry=registry,
                orderbook_reader=market_ws.snapshot,
            ),
            trading_service=TradingService(executor=_NoFillExecutor()),
            event_bus=None,
        )
    )
    return await service.list_sports_tail_candidates(limit=2, offset=0)


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


async def _run_worker_with_live_state_entry_signal():
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
        trading_service=TradingService(executor=_NoFillExecutor()),
        portfolio_budget_usdc=Decimal("10"),
        available_usdc=Decimal("10"),
        balance_usdc=Decimal("10"),
        allowance_usdc=Decimal("10"),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("10"),
        max_total_usdc=Decimal("10"),
        max_open_orders=10,
        order_retry_limit=2,
        entry_metadata_provider=lambda event, snapshot: {
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
    return await worker.process_event(
        DomainEvent(
            trace_id="trace-worker-entry-signal",
            event_type=DomainEventType.ENTRY_SIGNAL_TRIGGERED,
            event_id="event-worker-entry-signal",
            market_slug=market.market_slug,
            condition_id=market.condition_id,
            token_id="over",
            reason="sports_live_state_updated",
            created_at=orderbook.received_at,
            payload={"source": "unit_test"},
        )
    )
