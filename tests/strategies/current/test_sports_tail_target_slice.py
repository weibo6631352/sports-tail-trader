from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.app.entry_plan import EntryPlan
from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.app.trading_service import TradingService
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.allocation import Allocation, AllocationPlan
from polymarket_trader.domain.events import DomainEvent, DomainEventType, Fill
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderStatus,
    OrderType,
)
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position
from polymarket_trader.domain.order import Order
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.trading_decision_worker import TradingDecisionWorker
from polymarket_trader.extension_api import ExtensionContext, MarketTokenView

from strategies.current.config import CurrentStrategyConfig, sports_tail_policy_from_config
from strategies.current.outcomes import SportsMarketFamily, describe_sports_market
from strategies.current.recovery import decide_recovery
from strategies.current.sports_tail import (
    ExecutionPermission,
    LiveGameStatus,
    SportsMarketSnapshot,
    SportsMarketSide,
    SportsMarketType,
    TailAction,
    evaluate_tail_opportunity,
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
        SportsMarketType.BINARY_PROP,
    )
    assert policy.totals_execution_permission == ExecutionPermission.AUTO_EXECUTE
    assert policy.moneyline_execution_permission == ExecutionPermission.AUTO_EXECUTE
    assert policy.spreads_execution_permission == ExecutionPermission.AUTO_EXECUTE
    assert policy.tennis_locked_moneyline_max_entry_price == Decimal("0.995")
    assert policy.baseball_max_game_state_age_seconds == 45


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


def test_universe_accepts_tennis_first_set_totals_as_single_game_market() -> None:
    market = Market(
        condition_id="tennis-first-set-total-condition",
        market_slug="atp-ghibaud-pieri-2026-04-29-first-set-total-9pt5",
        market_question="Ghibaudo vs. Pieri: Set 1 Games O/U 9.5",
        event_title="Shymkent 2: Antoine Ghibaudo vs Samuele Pieri",
        event_slug="atp-ghibaud-pieri-2026-04-29",
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="first-set-over", outcome="Over"),
            MarketOutcome(token_id="first-set-under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = select_market(CurrentStrategyConfig(), market)
    descriptor = describe_sports_market(market)

    assert decision.selected is True
    assert descriptor.market_family == SportsMarketFamily.SINGLE_GAME
    assert descriptor.market_type == SportsMarketType.TOTALS
    assert descriptor.line == Decimal("9.5")


def test_universe_accepts_single_game_binary_props_for_whole_market_coverage() -> None:
    market = _single_game_binary_prop_market()

    decision = select_market(CurrentStrategyConfig(), market)
    descriptor = describe_sports_market(market)

    assert descriptor.accepted is True
    assert descriptor.market_family == SportsMarketFamily.SINGLE_GAME
    assert descriptor.market_type == SportsMarketType.BINARY_PROP
    assert [target.side for target in descriptor.targets] == [SportsMarketSide.YES, SportsMarketSide.NO]
    assert decision.selected is True


def test_single_game_binary_props_are_record_only_until_specific_model_exists() -> None:
    game = live_game_state_from_metadata({"sports_tail_game": _moneyline_live_game()})
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.BINARY_PROP,
        side=SportsMarketSide.YES,
        token_id="goal-yes",
        line=None,
        best_ask=Decimal("0.20"),
        buyable_liquidity_usdc=Decimal("20"),
        market_family=SportsMarketFamily.SINGLE_GAME,
        market_slug="soccer-ars-che-2026-04-30-first-goal-yes-no",
        market_end_date=datetime(2026, 4, 27, 0, 30, tzinfo=timezone.utc),
    )

    result = evaluate_tail_opportunity(
        game,
        market,
        policy=sports_tail_policy_from_config(CurrentStrategyConfig()),
        now=datetime(2026, 4, 27, 0, 0, 5, tzinfo=timezone.utc),
    )

    assert result.accepted is True
    assert result.action == TailAction.RECORD
    assert result.reason == "binary_prop_requires_specific_model"


def test_player_next_team_yes_no_is_outright_not_single_game_binary_prop() -> None:
    market = Market(
        condition_id="player-next-team-condition",
        market_slug="will-lebron-james-play-for-the-miami-heat-in-2026-27",
        market_question="Will LeBron James play for the Miami Heat in 2026-27?",
        event_title="NBA: LeBron James Next Team",
        event_slug="nba-lebron-james-next-team",
        category="Sports",
        tags=("NBA", "Basketball"),
        outcomes=(
            MarketOutcome(token_id="yes", outcome="Yes"),
            MarketOutcome(token_id="no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    descriptor = describe_sports_market(market)
    decision = select_market(CurrentStrategyConfig(), market)

    assert descriptor.accepted is True
    assert descriptor.market_type == SportsMarketType.BINARY_PROP
    assert descriptor.market_family == SportsMarketFamily.OUTRIGHT
    assert decision.selected is False
    assert decision.reason == "outright_market_not_auto_tradable"


def test_league_winner_yes_no_is_outright_not_single_game_binary_prop() -> None:
    market = _league_winner_market()

    descriptor = describe_sports_market(market)
    decision = select_market(CurrentStrategyConfig(), market)

    assert descriptor.accepted is True
    assert descriptor.market_type == SportsMarketType.BINARY_PROP
    assert descriptor.market_family == SportsMarketFamily.OUTRIGHT
    assert decision.selected is False
    assert decision.reason == "outright_market_not_auto_tradable"


def test_league_winner_with_team_outcomes_is_outright_market() -> None:
    market = Market(
        condition_id="league-winner-team-outcomes-condition",
        market_slug="2026-soccer-japan-j-league-winner-v-varen-nagasaki",
        market_question="Will V-Varen Nagasaki win Japan J. League?",
        event_title="Japan J. League: Winner",
        event_slug="2026-soccer-japan-j-league-winner",
        category="Sports",
        tags=("Soccer", "Japan J League"),
        outcomes=(
            MarketOutcome(token_id="team", outcome="V-Varen Nagasaki"),
            MarketOutcome(token_id="field", outcome="Field"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    descriptor = describe_sports_market(market)
    decision = select_market(CurrentStrategyConfig(), market)

    assert descriptor.accepted is True
    assert descriptor.market_family == SportsMarketFamily.OUTRIGHT
    assert decision.selected is False
    assert decision.reason == "outright_market_not_auto_tradable"


def test_award_draft_trade_and_cba_props_are_outright_binary_props() -> None:
    markets = (
        Market(
            condition_id="award-condition",
            market_slug="will-aaron-judge-win-the-2026-american-league-mvp-award",
            market_question="Will Aaron Judge win the 2026 American League MVP Award?",
            event_title="MLB: 2026 AL MVP",
            event_slug="pro-baseball-2026-al-mvp",
            category="Sports",
            tags=("MLB", "Awards"),
            outcomes=(MarketOutcome(token_id="award-yes", outcome="Yes"), MarketOutcome(token_id="award-no", outcome="No")),
            trading_status=TradingStatus.ELIGIBLE,
        ),
        Market(
            condition_id="draft-condition",
            market_slug="will-aj-dybantsa-be-the-first-pick-in-the-2026-nba-draft",
            market_question="Will AJ Dybantsa be the first pick in the 2026 NBA draft?",
            event_title="2026 NBA Draft: 1st Overall pick",
            event_slug="2026-nba-draft-1st-overall-pick",
            category="Sports",
            tags=("NBA", "NBA Draft"),
            outcomes=(MarketOutcome(token_id="draft-yes", outcome="Yes"), MarketOutcome(token_id="draft-no", outcome="No")),
            trading_status=TradingStatus.ELIGIBLE,
        ),
        Market(
            condition_id="trade-condition",
            market_slug="will-aj-brown-be-traded",
            market_question="Will AJ Brown be traded?",
            event_title="Which NFL players will be traded?",
            event_slug="which-nfl-players-will-be-traded",
            category="Sports",
            tags=("NFL", "trade"),
            outcomes=(MarketOutcome(token_id="trade-yes", outcome="Yes"), MarketOutcome(token_id="trade-no", outcome="No")),
            trading_status=TradingStatus.ELIGIBLE,
        ),
        Market(
            condition_id="leave-team-condition",
            market_slug="nba-stephen-curry-to-leave-warriors",
            market_question="NBA: Stephen Curry to leave Warriors?",
            event_title="NBA: Stephen Curry to leave Warriors?",
            event_slug="nba-stephen-curry-to-leave-warriors",
            category="Sports",
            tags=("NBA", "Basketball", "Stephen Curry", "Sports"),
            outcomes=(
                MarketOutcome(token_id="leave-yes", outcome="Yes"),
                MarketOutcome(token_id="leave-no", outcome="No"),
            ),
            trading_status=TradingStatus.ELIGIBLE,
        ),
        Market(
            condition_id="cba-condition",
            market_slug="new-mlb-cba-by-dec-1",
            market_question="New MLB CBA by Dec. 1?",
            event_title="New MLB CBA by Dec. 1?",
            event_slug="new-mlb-cba-by-dec-1",
            category="Sports",
            tags=("MLB", "Collective bargaining"),
            outcomes=(MarketOutcome(token_id="cba-yes", outcome="Yes"), MarketOutcome(token_id="cba-no", outcome="No")),
            trading_status=TradingStatus.ELIGIBLE,
        ),
    )

    descriptors = tuple(describe_sports_market(market) for market in markets)
    decisions = tuple(select_market(CurrentStrategyConfig(), market) for market in markets)

    assert [descriptor.market_type for descriptor in descriptors] == [SportsMarketType.BINARY_PROP] * 5
    assert [descriptor.market_family for descriptor in descriptors] == [SportsMarketFamily.OUTRIGHT] * 5
    assert [decision.selected for decision in decisions] == [False] * 5


def test_hyphenated_draft_prop_with_placeholder_v_is_not_single_game() -> None:
    market = Market(
        condition_id="draft-placeholder-condition",
        market_slug="will-player-v-be-the-first-pick-in-the-2026-nba-draft",
        market_question="Will Player V be the first pick in the 2026 NBA draft?",
        event_title="2026 NBA Draft: 1st Overall pick",
        event_slug="2026-nba-draft-1st-overall-pick",
        category="Sports",
        tags=("NBA", "NBA Draft"),
        outcomes=(
            MarketOutcome(token_id="yes", outcome="Yes"),
            MarketOutcome(token_id="no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    descriptor = describe_sports_market(market)
    decision = select_market(CurrentStrategyConfig(), market)

    assert descriptor.market_family == SportsMarketFamily.OUTRIGHT
    assert decision.selected is False
    assert decision.reason == "outright_market_not_auto_tradable"


def test_playoff_advance_props_are_not_single_game_live_markets() -> None:
    market = Market(
        condition_id="playoff-advance-condition",
        market_slug="will-boston-celtics-advance-to-the-conference-finals-in-the-2026-nba-playoffs",
        market_question="Will the Boston Celtics advance to the Conference Finals in the 2026 NBA Playoffs?",
        event_title="NBA Playoffs: Team to advance to Conference Finals",
        event_slug="nba-playoffs-team-to-advance-to-conference-finals",
        category="Sports",
        tags=("NBA", "2026 NBA Playoffs", "Conference Finals"),
        outcomes=(
            MarketOutcome(token_id="yes", outcome="Yes"),
            MarketOutcome(token_id="no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    descriptor = describe_sports_market(market)
    decision = select_market(CurrentStrategyConfig(), market)

    assert descriptor.market_family == SportsMarketFamily.OUTRIGHT
    assert decision.selected is False
    assert decision.reason == "outright_market_not_auto_tradable"


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


def test_universe_accepts_kbo_market_when_live_source_supports_baseball() -> None:
    market = Market(
        condition_id="kbo-condition",
        market_slug="kbo-kia-nc-2026-04-28",
        market_question="KBO: Kia Tigers vs. NC Dinos",
        event_title="KBO: Kia Tigers vs. NC Dinos",
        event_slug="kbo-kia-nc-2026-04-28",
        category=None,
        tags=(),
        outcomes=(
            MarketOutcome(token_id="kia", outcome="Kia Tigers"),
            MarketOutcome(token_id="nc", outcome="NC Dinos"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = select_market(CurrentStrategyConfig(), market)

    assert decision.selected is True
    assert decision.reason == "sports_market_selected"


def test_universe_accepts_wtt_table_tennis_market_for_live_state_coverage() -> None:
    market = Market(
        condition_id="wtt-condition",
        market_slug="wttmen-austria-italy-2026-04-30",
        market_question="WTT - Men's Singles: Austria vs Italy",
        event_title="WTT - Men's Singles: Austria vs Italy",
        event_slug="wttmen-austria-italy-2026-04-30",
        category=None,
        tags=("Sports", "Table Tennis", "WTT"),
        outcomes=(
            MarketOutcome(token_id="austria", outcome="Austria"),
            MarketOutcome(token_id="italy", outcome="Italy"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = select_market(CurrentStrategyConfig(), market)
    descriptor = describe_sports_market(market)

    assert decision.selected is True
    assert decision.reason == "sports_market_selected"
    assert descriptor.accepted is True
    assert descriptor.market_family == SportsMarketFamily.SINGLE_GAME


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


def test_real_polymarket_top_goalscorer_yes_no_is_classified_as_outright_binary_prop() -> None:
    market = _epl_top_goalscorer_yes_no_market()

    descriptor = describe_sports_market(market)
    decision = select_market(CurrentStrategyConfig(), market)

    assert descriptor.accepted is True
    assert descriptor.market_family == SportsMarketFamily.OUTRIGHT
    assert descriptor.market_type == SportsMarketType.BINARY_PROP
    assert [(target.side, target.label) for target in descriptor.targets] == [
        (SportsMarketSide.YES, "Yes"),
        (SportsMarketSide.NO, "No"),
    ]
    assert decision.selected is False
    assert decision.reason == "outright_market_not_auto_tradable"


def test_real_polymarket_ucl_winner_yes_no_is_classified_as_outright_binary_prop() -> None:
    market = _ucl_winner_yes_no_market()

    descriptor = describe_sports_market(market)
    decision = select_market(CurrentStrategyConfig(), market)

    assert descriptor.accepted is True
    assert descriptor.market_family == SportsMarketFamily.OUTRIGHT
    assert descriptor.market_type == SportsMarketType.BINARY_PROP
    assert [target.side for target in descriptor.targets] == [SportsMarketSide.YES, SportsMarketSide.NO]
    assert decision.selected is False
    assert decision.reason == "outright_market_not_auto_tradable"


def test_real_polymarket_homepage_nba_champion_yes_no_is_classified_as_outright_binary_prop() -> None:
    market = _nba_champion_yes_no_market()

    descriptor = describe_sports_market(market)
    decision = select_market(CurrentStrategyConfig(), market)

    assert descriptor.accepted is True
    assert descriptor.market_family == SportsMarketFamily.OUTRIGHT
    assert descriptor.market_type == SportsMarketType.BINARY_PROP
    assert [target.side for target in descriptor.targets] == [SportsMarketSide.YES, SportsMarketSide.NO]
    assert decision.selected is False
    assert decision.reason == "outright_market_not_auto_tradable"


def test_real_polymarket_season_award_and_leader_props_are_outright_binary_props() -> None:
    markets = (
        Market(
            condition_id="mls-defender-award",
            market_slug="will-aaron-long-win-2026-mls-defender-of-the-year",
            market_question="Will Aaron Long win 2026 MLS Defender of the Year?",
            event_title="2026 MLS Defender of the Year",
            event_slug="2026-mls-defender-of-the-year",
            category="Sports",
            tags=("Sports", "MLS", "Soccer"),
            outcomes=(
                MarketOutcome(token_id="defender-yes", outcome="Yes"),
                MarketOutcome(token_id="defender-no", outcome="No"),
            ),
            trading_status=TradingStatus.ELIGIBLE,
        ),
        Market(
            condition_id="uel-most-goals",
            market_slug="will-abde-ezzalzouli-score-the-most-goals-in-the-2025-26-uefa-europa-league",
            market_question="Will Abde Ezzalzouli score the most goals in the 2025-26 UEFA Europa League?",
            event_title="2025-26 UEFA Europa League: Most Goals",
            event_slug="2025-26-uefa-europa-league-most-goals",
            category="Sports",
            tags=("Sports", "Soccer", "Europa League", "Goals"),
            outcomes=(
                MarketOutcome(token_id="goals-yes", outcome="Yes"),
                MarketOutcome(token_id="goals-no", outcome="No"),
            ),
            trading_status=TradingStatus.ELIGIBLE,
        ),
    )

    for market in markets:
        descriptor = describe_sports_market(market)
        decision = select_market(CurrentStrategyConfig(), market)

        assert descriptor.accepted is True
        assert descriptor.market_family == SportsMarketFamily.OUTRIGHT
        assert descriptor.market_type == SportsMarketType.BINARY_PROP
        assert decision.selected is False
        assert decision.reason == "outright_market_not_auto_tradable"


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


def test_ended_moneyline_can_create_buy_before_polymarket_closes_market() -> None:
    market = _moneyline_market()
    orderbook = _orderbook(token_id="home", best_ask=Decimal("0.96"))

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-ended-moneyline",
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
                    "home_score": 111,
                    "away_score": 104,
                    "period": "Final",
                    "seconds_remaining": 0,
                    "status": "ended",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.token_id == "home"
    assert decision.metadata["sports_tail_reason"] == "ended_not_closed_moneyline"
    assert decision.metadata["sports_tail_opportunity_type"] == "ended_not_closed"


def test_ended_totals_under_can_create_buy_when_final_score_is_below_line() -> None:
    market = _totals_market()
    orderbook = _orderbook(token_id="under", best_ask=Decimal("0.98"))

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-ended-under",
            market=market,
            token_id="under",
            orderbook=orderbook,
            amount_usdc=Decimal("10"),
            now=datetime(2026, 4, 27, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "NHL",
                    "home_name": "TB",
                    "away_name": "MON",
                    "home_score": 2,
                    "away_score": 1,
                    "period": "Final",
                    "seconds_remaining": 0,
                    "status": "ended",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.token_id == "under"
    assert decision.metadata["sports_tail_reason"] == "ended_not_closed_totals_under"
    assert decision.metadata["sports_tail_opportunity_type"] == "ended_not_closed"


def test_ended_tennis_first_set_total_is_not_treated_as_total_sets_under() -> None:
    market = Market(
        condition_id="tennis-first-set-total-condition",
        market_slug="wta-ren-hercog-2026-04-29-first-set-total-9pt5",
        market_question="Yufei Ren vs Polona Hercog first set total 9.5",
        event_title="Huzhou: Yufei Ren vs Polona Hercog",
        event_slug="wta-ren-hercog-2026-04-29",
        category="Sports",
        tags=("WTA", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="first-set-over", outcome="Over"),
            MarketOutcome(token_id="first-set-under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-ended-first-set-total-under",
            market=market,
            token_id="first-set-under",
            orderbook=_orderbook(token_id="first-set-under", best_ask=Decimal("0.98")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 30, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "WTA",
                    "home_name": "Yufei Ren",
                    "away_name": "Polona Hercog",
                    "home_score": 2,
                    "away_score": 0,
                    "period": "Final",
                    "seconds_remaining": 0,
                    "status": "ended",
                    "observed_at": "2026-04-30T00:00:00+00:00",
                    "tennis_state": {
                        "home_sets_won": 2,
                        "away_sets_won": 0,
                        "home_total_games": 12,
                        "away_total_games": 5,
                        "total_games": 17,
                        "set_scores": ((6, 4), (6, 1)),
                    },
                }
            },
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "outcome_not_locked"


def test_ended_tennis_first_set_total_under_uses_first_set_score() -> None:
    market = _tennis_first_set_total_market()

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-ended-first-set-total-under-supported",
            market=market,
            token_id="first-set-under",
            orderbook=_orderbook(token_id="first-set-under", best_ask=Decimal("0.98")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 30, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "WTA",
                    "home_name": "Yufei Ren",
                    "away_name": "Polona Hercog",
                    "home_score": 2,
                    "away_score": 0,
                    "period": "Final",
                    "seconds_remaining": 0,
                    "status": "ended",
                    "observed_at": "2026-04-30T00:00:00+00:00",
                    "tennis_state": {
                        "home_sets_won": 2,
                        "away_sets_won": 0,
                        "home_total_games": 12,
                        "away_total_games": 5,
                        "total_games": 17,
                        "set_scores": ((6, 1), (6, 4)),
                    },
                }
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.token_id == "first-set-under"
    assert decision.metadata["sports_tail_reason"] == "ended_not_closed_tennis_set_games_under"


def test_ended_tennis_first_set_total_under_rejects_when_first_set_went_over() -> None:
    market = _tennis_first_set_total_market()

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-ended-first-set-total-under-over-score",
            market=market,
            token_id="first-set-under",
            orderbook=_orderbook(token_id="first-set-under", best_ask=Decimal("0.98")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 30, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "WTA",
                    "home_name": "Yufei Ren",
                    "away_name": "Polona Hercog",
                    "home_score": 2,
                    "away_score": 0,
                    "period": "Final",
                    "seconds_remaining": 0,
                    "status": "ended",
                    "observed_at": "2026-04-30T00:00:00+00:00",
                    "tennis_state": {
                        "home_sets_won": 2,
                        "away_sets_won": 0,
                        "home_total_games": 12,
                        "away_total_games": 5,
                        "total_games": 17,
                        "set_scores": ((6, 4), (6, 1)),
                    },
                }
            },
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "outcome_not_locked"


def test_live_tennis_first_set_total_over_uses_current_set_score() -> None:
    market = _tennis_first_set_total_market()

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-live-first-set-total-over-supported",
            market=market,
            token_id="first-set-over",
            orderbook=_orderbook(token_id="first-set-over", best_ask=Decimal("0.98")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 30, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "WTA",
                    "home_name": "Yufei Ren",
                    "away_name": "Polona Hercog",
                    "home_score": 0,
                    "away_score": 0,
                    "period": "S1",
                    "status": "live",
                    "observed_at": "2026-04-30T00:00:00+00:00",
                    "tennis_state": {
                        "current_set": 1,
                        "home_current_set_games": 5,
                        "away_current_set_games": 5,
                        "home_total_games": 5,
                        "away_total_games": 5,
                        "total_games": 10,
                        "set_scores": ((5, 5),),
                    },
                }
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.token_id == "first-set-over"
    assert decision.metadata["sports_tail_reason"] == "tennis_set_games_over_locked"
    assert decision.metadata["scope_type"] == "tennis_set_games"
    assert decision.metadata["scope_number"] == 1


def test_period_total_market_is_not_treated_as_full_game_total() -> None:
    market = Market(
        condition_id="first-quarter-total-condition",
        market_slug="nba-nyk-bos-2026-04-30-first-quarter-total-51pt5",
        market_question="NYK vs BOS first quarter total 51.5",
        event_title="NYK vs BOS",
        event_slug="nba-nyk-bos-2026-04-30",
        category="Sports",
        tags=("NBA", "Basketball"),
        outcomes=(
            MarketOutcome(token_id="first-quarter-over", outcome="Over"),
            MarketOutcome(token_id="first-quarter-under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-period-total-unsupported",
            market=market,
            token_id="first-quarter-over",
            orderbook=_orderbook(token_id="first-quarter-over", best_ask=Decimal("0.98")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 30, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "NBA",
                    "home_name": "NYK",
                    "away_name": "BOS",
                    "home_score": 120,
                    "away_score": 118,
                    "period": "Final",
                    "seconds_remaining": 0,
                    "status": "ended",
                    "observed_at": "2026-04-30T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "unsupported_market_scope"


def test_ended_moneyline_tie_is_not_traded_as_deterministic_result() -> None:
    market = _moneyline_market()
    orderbook = _orderbook(token_id="home", best_ask=Decimal("0.96"))

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-ended-tie",
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
                    "home_score": 104,
                    "away_score": 104,
                    "period": "Final",
                    "seconds_remaining": 0,
                    "status": "ended",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "outcome_not_locked"


def test_moneyline_uses_live_home_away_names_instead_of_outcome_order() -> None:
    market = Market(
        condition_id="kbo-condition",
        market_slug="kbo-kia-nc-2026-04-28",
        market_question="KBO: Kia Tigers vs. NC Dinos",
        event_title="KBO: Kia Tigers vs. NC Dinos",
        category="Sports",
        tags=("KBO",),
        outcomes=(
            MarketOutcome(token_id="kia", outcome="Kia Tigers"),
            MarketOutcome(token_id="nc", outcome="NC Dinos"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    metadata = {
        "sports_tail_game": {
            "league": "KBO",
            "home_name": "NC Dinos",
            "away_name": "Kia Tigers",
            "home_score": 5,
            "away_score": 4,
            "period": "Final",
            "seconds_remaining": 0,
            "status": "ended",
            "observed_at": "2026-04-28T12:55:00+00:00",
        },
        "sports_live_match": {
            "matched_home_alias": "NC Dinos",
            "matched_away_alias": "Kia Tigers",
        },
    }

    config = CurrentStrategyConfig(sports_min_liquidity_usdc=Decimal("0.01"))

    losing_token = decide_entry(
        config,
        ExtensionContext(
            trace_id="trace-kbo-loser",
            market=market,
            token_id="kia",
            orderbook=_orderbook(token_id="kia", best_ask=Decimal("0.01")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 28, 12, 55, tzinfo=timezone.utc),
            metadata=metadata,
        ),
    )
    winning_token = decide_entry(
        config,
        ExtensionContext(
            trace_id="trace-kbo-winner",
            market=market,
            token_id="nc",
            orderbook=_orderbook(token_id="nc", best_ask=Decimal("0.96")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 28, 12, 55, tzinfo=timezone.utc),
            metadata=metadata,
        ),
    )

    assert losing_token.action.value == "skip"
    assert losing_token.reason == "outcome_not_locked"
    assert winning_token.action.value == "buy"
    assert winning_token.token_id == "nc"
    assert winning_token.metadata["sports_tail_reason"] == "ended_not_closed_moneyline"


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
    assert plan.metadata["sports_exit_mode"] == "profit_take"
    assert plan.metadata["sports_exit_plan"]["primary_action"] == "place_profit_take_gtc_sell_after_buy_fill"


def test_follow_up_waits_for_settlement_by_default() -> None:
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

    assert decisions == ()


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
                    "away_sets_won": 1,
                    "current_set": 2,
                    "home_current_set_games": 3,
                    "away_current_set_games": 5,
                    "home_total_games": 6,
                    "away_total_games": 11,
                    "set_scores": ((3, 6), (3, 5)),
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


def test_tennis_moneyline_first_set_lead_is_not_tail_enough() -> None:
    market = _tennis_moneyline_market()

    decision = decide_entry(
        CurrentStrategyConfig(sports_min_liquidity_usdc=Decimal("0.01")),
        ExtensionContext(
            trace_id="trace-tennis-first-set",
            market=market,
            token_id="tennis-home",
            orderbook=_orderbook(token_id="tennis-home", best_ask=Decimal("0.56")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    **_tennis_near_locked_live_game(),
                    "tennis_state": {
                        "home_sets_won": 0,
                        "away_sets_won": 0,
                        "current_set": 1,
                        "home_current_set_games": 5,
                        "away_current_set_games": 3,
                        "home_total_games": 5,
                        "away_total_games": 3,
                        "set_scores": ((5, 3),),
                        "home_point": "40",
                        "away_point": "0",
                        "serving_side": "home",
                    },
                }
            },
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "tennis_not_late_enough"


def test_tennis_moneyline_requires_current_set_tail_after_set_lead() -> None:
    market = _tennis_moneyline_market()

    decision = decide_entry(
        CurrentStrategyConfig(sports_min_liquidity_usdc=Decimal("0.01")),
        ExtensionContext(
            trace_id="trace-tennis-too-early-second-set",
            market=market,
            token_id="tennis-home",
            orderbook=_orderbook(token_id="tennis-home", best_ask=Decimal("0.56")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    **_tennis_near_locked_live_game(),
                    "tennis_state": {
                        "home_sets_won": 1,
                        "away_sets_won": 0,
                        "current_set": 2,
                        "home_current_set_games": 3,
                        "away_current_set_games": 2,
                        "home_total_games": 9,
                        "away_total_games": 5,
                        "set_scores": ((6, 3), (3, 2)),
                    },
                }
            },
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "tennis_not_late_enough"


def test_tennis_moneyline_tail_bypasses_far_gamma_end_date() -> None:
    now = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
    market = _tennis_moneyline_market().with_metadata(
        end_date=datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc)
    )

    decision = decide_entry(
        CurrentStrategyConfig(sports_min_liquidity_usdc=Decimal("0.01")),
        ExtensionContext(
            trace_id="trace-tennis-tail-far-gamma-end",
            market=market,
            token_id="tennis-home",
            orderbook=_orderbook(token_id="tennis-home", best_ask=Decimal("0.56")),
            amount_usdc=Decimal("5"),
            now=now,
            metadata={"sports_tail_game": _tennis_near_locked_live_game()},
        ),
    )

    assert decision.action.value == "buy"
    assert decision.reason == "strategy_entry"
    assert decision.metadata["sports_tail_reason"] == "tennis_moneyline_near_locked"


def test_low_settlement_efficiency_entry_uses_profit_take_exit_plan_when_viable() -> None:
    market = _totals_market().with_tick_size(Decimal("0.01"))

    decision = decide_entry(
        CurrentStrategyConfig(
            sports_min_liquidity_usdc=Decimal("0.01"),
            sports_min_expected_profit_usdc=Decimal("0.10"),
            sports_min_expected_profit_per_hour_usdc=Decimal("0.10"),
            sports_profit_take_min_profit_usdc=Decimal("0.03"),
        ),
        ExtensionContext(
            trace_id="trace-profit-take-entry",
            market=market,
            token_id="over",
            orderbook=_orderbook(token_id="over", best_ask=Decimal("0.98")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 27, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "NHL",
                    "home_name": "TB",
                    "away_name": "MON",
                    "home_score": 3,
                    "away_score": 2,
                    "period": "P3",
                    "seconds_remaining": 1800,
                    "status": "live",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.metadata["sports_exit_mode"] == "profit_take"
    assert decision.metadata["sports_profit_take_target_price"] == "0.99"
    assert decision.metadata["sports_profit_take_expected_profit_usdc"] == "0.051020408163265306"
    assert decision.metadata["sports_expected_settlement_profit_usdc"] == "0.102040816326530612"


def test_settlement_efficient_entry_adds_profit_take_overlay_when_viable() -> None:
    market = Market(
        condition_id="mlb-settlement-overlay-condition",
        market_slug="mlb-ari-mil-2026-04-29",
        market_question="Arizona Diamondbacks vs. Milwaukee Brewers",
        event_title="Arizona Diamondbacks vs. Milwaukee Brewers",
        event_slug="mlb-ari-mil-2026-04-29",
        category="Sports",
        tags=("MLB",),
        outcomes=(
            MarketOutcome(token_id="diamondbacks", outcome="Arizona Diamondbacks"),
            MarketOutcome(token_id="brewers", outcome="Milwaukee Brewers"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    ).with_tick_size(Decimal("0.01"))

    decision = decide_entry(
        CurrentStrategyConfig(
            sports_min_liquidity_usdc=Decimal("0.01"),
            sports_min_expected_profit_usdc=Decimal("0.03"),
            sports_min_expected_profit_per_hour_usdc=Decimal("0.10"),
            sports_profit_take_min_profit_usdc=Decimal("0.02"),
        ),
        ExtensionContext(
            trace_id="trace-settlement-profit-take-overlay",
            market=market,
            token_id="diamondbacks",
            orderbook=_orderbook(token_id="diamondbacks", best_ask=Decimal("0.93")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 27, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "MLB",
                    "home_name": "Brewers",
                    "away_name": "Diamondbacks",
                    "home_score": 2,
                    "away_score": 4,
                    "period": "T8",
                    "seconds_remaining": None,
                    "status": "live",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                    "source": "mlb",
                    "baseball_state": {
                        "current_inning": 8,
                        "inning_half": "top",
                        "outs": 1,
                        "offense_team": "Arizona Diamondbacks",
                        "defense_team": "Milwaukee Brewers",
                        "occupied_bases": (),
                    },
                }
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.metadata["sports_exit_mode"] == "settlement"
    assert decision.metadata["sports_profit_take_overlay_enabled"] is True
    assert decision.metadata["sports_profit_take_target_price"] == "0.94"
    assert decision.metadata["sports_profit_take_expected_profit_usdc"] == "0.053763440860215054"
    assert decision.metadata["sports_exit_plan"]["primary_action"] == "place_profit_take_gtc_sell_after_buy_fill"


def test_low_settlement_efficiency_entry_rejects_when_profit_take_is_not_viable() -> None:
    market = _totals_market().with_tick_size(Decimal("0.01"))

    decision = decide_entry(
        CurrentStrategyConfig(
            sports_min_liquidity_usdc=Decimal("0.01"),
            sports_min_expected_profit_usdc=Decimal("0.10"),
            sports_min_expected_profit_per_hour_usdc=Decimal("2"),
            sports_profit_take_min_profit_usdc=Decimal("0.06"),
        ),
        ExtensionContext(
            trace_id="trace-profit-take-reject",
            market=market,
            token_id="over",
            orderbook=_orderbook(token_id="over", best_ask=Decimal("0.98")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 27, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "NHL",
                    "home_name": "TB",
                    "away_name": "MON",
                    "home_score": 3,
                    "away_score": 2,
                    "period": "P3",
                    "seconds_remaining": 1800,
                    "status": "live",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "profit_take_not_viable"
    assert decision.metadata["sports_profit_take_expected_profit_usdc"] == "0.051020408163265306"


def test_low_profit_entry_uses_profit_take_when_hourly_capital_efficiency_is_high() -> None:
    market = _totals_market().with_tick_size(Decimal("0.001"))

    decision = decide_entry(
        CurrentStrategyConfig(
            sports_min_liquidity_usdc=Decimal("0.01"),
            sports_min_expected_profit_usdc=Decimal("0.10"),
            sports_min_expected_profit_per_hour_usdc=Decimal("0.10"),
            sports_profit_take_min_profit_usdc=Decimal("0.02"),
            sports_totals_max_entry_price=Decimal("0.999"),
            sports_profit_take_hold_minutes=2,
        ),
        ExtensionContext(
            trace_id="trace-profit-take-hourly-efficiency",
            market=market,
            token_id="over",
            orderbook=_orderbook(token_id="over", best_ask=Decimal("0.995")),
            amount_usdc=Decimal("5"),
            now=datetime(2026, 4, 27, tzinfo=timezone.utc),
            metadata={
                "sports_tail_game": {
                    "league": "NHL",
                    "home_name": "TB",
                    "away_name": "MON",
                    "home_score": 3,
                    "away_score": 2,
                    "period": "P3",
                    "seconds_remaining": 1800,
                    "status": "live",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.metadata["sports_exit_mode"] == "profit_take"
    assert decision.metadata["sports_profit_take_target_price"] == "0.996"
    assert decision.metadata["sports_profit_take_expected_profit_usdc"] == "0.005025125628140704"
    assert decision.metadata["sports_profit_take_expected_profit_per_hour_usdc"] == "0.150753768844221106"
    assert decision.metadata["sports_capital_efficiency_reason"] == "profit_take_hourly_efficiency_high"


def test_tennis_match_total_over_uses_minimum_possible_final_games_in_deciding_set() -> None:
    now = datetime(2026, 4, 29, 8, 25, tzinfo=timezone.utc)
    market = Market(
        condition_id="tennis-match-total-condition",
        market_slug="atp-erhard-nedic-2026-04-29-match-total-23pt5",
        market_question="Mathys Erhard vs Andrej Nedic match total 23.5",
        event_title="Shymkent 2: Mathys Erhard vs Andrej Nedic",
        event_slug="atp-erhard-nedic-2026-04-29",
        end_date=datetime(2026, 5, 6, 6, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="tennis-total-over", outcome="Over"),
            MarketOutcome(token_id="tennis-total-under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = decide_entry(
        CurrentStrategyConfig(sports_min_liquidity_usdc=Decimal("0.01")),
        ExtensionContext(
            trace_id="trace-tennis-min-final-games-total",
            market=market,
            token_id="tennis-total-over",
            orderbook=_orderbook(token_id="tennis-total-over", best_ask=Decimal("0.95")),
            amount_usdc=Decimal("5"),
            now=now,
            metadata={
                "sports_tail_game": {
                    "league": "Shymkent II Kazakhstan",
                    "home_name": "Mathys Erhard",
                    "away_name": "Andrej Nedic",
                    "home_score": 1,
                    "away_score": 1,
                    "period": "S3",
                    "status": "live",
                    "observed_at": "2026-04-29T08:25:00+00:00",
                    "tennis_state": {
                        "home_sets_won": 1,
                        "away_sets_won": 1,
                        "current_set": 3,
                        "home_current_set_games": 0,
                        "away_current_set_games": 1,
                        "home_total_games": 10,
                        "away_total_games": 9,
                        "total_games": 19,
                        "set_scores": ((4, 6), (6, 2), (0, 1)),
                    },
                }
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.reason == "strategy_entry"
    assert decision.metadata["sports_tail_reason"] == "tennis_totals_over_min_final_games_locked"


def test_tennis_first_set_winner_current_set_near_locked_can_enter_before_set_ends() -> None:
    now = datetime(2026, 4, 29, 8, 41, tzinfo=timezone.utc)
    market = Market(
        condition_id="tennis-first-set-condition",
        market_slug="atp-gaston-blanc-2026-04-29-first-set-winner-Gaston-vs-Blanch",
        market_question="Hugo Gaston vs Darwin Blanch first set winner",
        event_title="Mauthausen: Hugo Gaston vs Darwin Blanch",
        event_slug="atp-gaston-blanc-2026-04-29",
        end_date=datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="gaston-first-set", outcome="Gaston"),
            MarketOutcome(token_id="blanch-first-set", outcome="Blanch"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = decide_entry(
        CurrentStrategyConfig(sports_min_liquidity_usdc=Decimal("0.01")),
        ExtensionContext(
            trace_id="trace-tennis-first-set-current-tail",
            market=market,
            token_id="gaston-first-set",
            orderbook=_orderbook(token_id="gaston-first-set", best_ask=Decimal("0.95")),
            amount_usdc=Decimal("5"),
            now=now,
            metadata={
                "sports_tail_game": {
                    "league": "Mauthausen, Austria",
                    "home_name": "Hugo Gaston",
                    "away_name": "Darwin Blanch",
                    "home_score": 0,
                    "away_score": 0,
                    "period": "S1",
                    "status": "live",
                    "observed_at": "2026-04-29T08:41:00+00:00",
                    "tennis_state": {
                        "home_sets_won": 0,
                        "away_sets_won": 0,
                        "current_set": 1,
                        "home_current_set_games": 5,
                        "away_current_set_games": 3,
                        "home_total_games": 5,
                        "away_total_games": 3,
                        "set_scores": ((5, 3),),
                        "home_point": "40",
                        "away_point": "0",
                        "serving_side": "home",
                    },
                }
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.reason == "strategy_entry"
    assert decision.metadata["sports_tail_reason"] == "tennis_set_winner_current_set_near_locked"


def test_tennis_first_set_winner_current_set_near_locked_requires_service_point_pressure() -> None:
    now = datetime(2026, 4, 29, 9, 29, tzinfo=timezone.utc)
    market = Market(
        condition_id="tennis-first-set-condition-no-pressure",
        market_slug="atp-gaston-blanc-2026-04-29-first-set-winner-Gaston-vs-Blanch",
        market_question="Hugo Gaston vs Darwin Blanch first set winner",
        event_title="Mauthausen: Hugo Gaston vs Darwin Blanch",
        event_slug="atp-gaston-blanc-2026-04-29",
        end_date=datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="gaston-first-set", outcome="Gaston"),
            MarketOutcome(token_id="blanch-first-set", outcome="Blanch"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = decide_entry(
        CurrentStrategyConfig(sports_min_liquidity_usdc=Decimal("0.01")),
        ExtensionContext(
            trace_id="trace-tennis-first-set-current-tail-no-pressure",
            market=market,
            token_id="gaston-first-set",
            orderbook=_orderbook(token_id="gaston-first-set", best_ask=Decimal("0.95")),
            amount_usdc=Decimal("5"),
            now=now,
            metadata={
                "sports_tail_game": {
                    "league": "Mauthausen, Austria",
                    "home_name": "Hugo Gaston",
                    "away_name": "Darwin Blanch",
                    "home_score": 0,
                    "away_score": 0,
                    "period": "S1",
                    "status": "live",
                    "observed_at": "2026-04-29T09:28:50+00:00",
                    "tennis_state": {
                        "home_sets_won": 0,
                        "away_sets_won": 0,
                        "current_set": 1,
                        "home_current_set_games": 5,
                        "away_current_set_games": 3,
                        "home_total_games": 5,
                        "away_total_games": 3,
                        "set_scores": ((5, 3),),
                        "home_point": "15",
                        "away_point": "15",
                        "serving_side": "away",
                    },
                }
            },
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "tennis_not_late_enough"


def test_tennis_first_set_winner_current_set_near_locked_can_place_maker_bid_below_one() -> None:
    now = datetime(2026, 4, 29, 9, 19, tzinfo=timezone.utc)
    market = Market(
        condition_id="tennis-first-set-near-lock-maker-condition",
        market_slug="atp-tomic-sharipo-2026-04-28-first-set-winner-Tomic-vs-Sharipov",
        market_question="Set 1 Winner: Tomic vs Sharipov",
        event_title="Jiujiang: Bernard Tomic vs Marat Sharipov",
        event_slug="atp-tomic-sharipo-2026-04-28",
        end_date=datetime(2026, 5, 5, 4, 30, tzinfo=timezone.utc),
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="tomic-first-set", outcome="Tomic"),
            MarketOutcome(token_id="sharipov-first-set", outcome="Sharipov"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = decide_entry(
        CurrentStrategyConfig(sports_min_liquidity_usdc=Decimal("0.01")),
        ExtensionContext(
            trace_id="trace-tennis-first-set-near-lock-maker",
            market=market,
            token_id="sharipov-first-set",
            orderbook=_orderbook(token_id="sharipov-first-set", best_ask=Decimal("1")),
            amount_usdc=Decimal("5"),
            now=now,
            metadata={
                "sports_tail_entry_signal_reason": "live_tail_state_candidate",
                "sports_tail_game": {
                    "league": "ATP Challenger Jiujiang, China Men Singles",
                    "home_name": "Bernard Tomic",
                    "away_name": "Marat Sharipov",
                    "home_score": 0,
                    "away_score": 0,
                    "period": "S1",
                    "status": "live",
                    "observed_at": "2026-04-29T09:18:58+00:00",
                    "tennis_state": {
                        "home_sets_won": 0,
                        "away_sets_won": 0,
                        "current_set": 1,
                        "home_current_set_games": 2,
                        "away_current_set_games": 5,
                        "home_total_games": 2,
                        "away_total_games": 5,
                        "set_scores": ((2, 5),),
                        "home_point": "0",
                        "away_point": "40",
                        "serving_side": "away",
                    },
                },
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.price == Decimal("0.97")
    assert decision.metadata["sports_tail_reason"] == "tennis_set_winner_current_set_near_locked"
    assert decision.metadata["sports_tail_maker_bid_reason"] == "ask_above_near_lock_price_cap"


def test_tennis_completed_set_winner_can_enter_at_locked_price_despite_wide_spread() -> None:
    now = datetime(2026, 4, 29, 8, 45, tzinfo=timezone.utc)
    market = Market(
        condition_id="tennis-first-set-locked-condition",
        market_slug="atp-ghibaud-pieri-2026-04-29-first-set-winner-Ghibaudo-vs-Pieri",
        market_question="Antoine Ghibaudo vs Samuele Pieri first set winner",
        event_title="Shymkent 2: Antoine Ghibaudo vs Samuele Pieri",
        event_slug="atp-ghibaud-pieri-2026-04-29",
        end_date=datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="ghibaudo-first-set", outcome="Ghibaudo"),
            MarketOutcome(token_id="pieri-first-set", outcome="Pieri"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    ).with_tick_size(Decimal("0.001"))

    decision = decide_entry(
        CurrentStrategyConfig(sports_min_liquidity_usdc=Decimal("0.01")),
        ExtensionContext(
            trace_id="trace-tennis-first-set-locked-wide-spread",
            market=market,
            token_id="pieri-first-set",
            orderbook=OrderbookSnapshot(
                token_id="pieri-first-set",
                best_bid=Decimal("0.001"),
                best_ask=Decimal("0.999"),
                bids=(PriceLevel(price=Decimal("0.001"), size=Decimal("20")),),
                asks=(PriceLevel(price=Decimal("0.999"), size=Decimal("20")),),
                received_at=now,
                condition_id="tennis-first-set-locked-condition",
            ),
            amount_usdc=Decimal("5"),
            now=now,
            metadata={
                "sports_tail_entry_signal_reason": "live_outcome_lock_candidate",
                "sports_tail_game": {
                    "league": "Shymkent 2, Kazakhstan",
                    "home_name": "Antoine Ghibaudo",
                    "away_name": "Samuele Pieri",
                    "home_score": 0,
                    "away_score": 1,
                    "period": "S2",
                    "status": "live",
                    "observed_at": "2026-04-29T08:45:00+00:00",
                    "tennis_state": {
                        "home_sets_won": 0,
                        "away_sets_won": 1,
                        "current_set": 2,
                        "home_current_set_games": 0,
                        "away_current_set_games": 0,
                        "home_total_games": 5,
                        "away_total_games": 7,
                        "set_scores": ((5, 7), (0, 0)),
                    },
                },
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.price == Decimal("0.995")
    assert decision.reason == "strategy_entry"
    assert decision.metadata["sports_tail_reason"] == "tennis_set_winner_locked"


def test_tennis_completed_set_winner_places_profitable_maker_bid_when_ask_is_one() -> None:
    now = datetime(2026, 4, 29, 8, 50, tzinfo=timezone.utc)
    market = Market(
        condition_id="tennis-first-set-ask-one-condition",
        market_slug="atp-gaston-blanc-2026-04-29-first-set-winner-Gaston-vs-Blanch",
        market_question="Hugo Gaston vs Darwin Blanch first set winner",
        event_title="Mauthausen: Hugo Gaston vs Darwin Blanch",
        event_slug="atp-gaston-blanc-2026-04-29",
        end_date=datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="gaston-first-set", outcome="Gaston"),
            MarketOutcome(token_id="blanch-first-set", outcome="Blanch"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    ).with_tick_size(Decimal("0.001"))

    decision = decide_entry(
        CurrentStrategyConfig(sports_min_liquidity_usdc=Decimal("0.01")),
        ExtensionContext(
            trace_id="trace-tennis-first-set-ask-one",
            market=market,
            token_id="gaston-first-set",
            orderbook=OrderbookSnapshot(
                token_id="gaston-first-set",
                best_bid=Decimal("0.001"),
                best_ask=Decimal("1"),
                bids=(PriceLevel(price=Decimal("0.001"), size=Decimal("20")),),
                asks=(PriceLevel(price=Decimal("1"), size=Decimal("20")),),
                received_at=now,
                condition_id="tennis-first-set-ask-one-condition",
            ),
            amount_usdc=Decimal("5"),
            now=now,
            metadata={
                "sports_tail_entry_signal_reason": "live_outcome_lock_candidate",
                "sports_tail_game": {
                    "league": "Mauthausen, Austria",
                    "home_name": "Hugo Gaston",
                    "away_name": "Darwin Blanch",
                    "home_score": 1,
                    "away_score": 0,
                    "period": "S2",
                    "status": "live",
                    "observed_at": "2026-04-29T08:50:00+00:00",
                    "tennis_state": {
                        "home_sets_won": 1,
                        "away_sets_won": 0,
                        "current_set": 2,
                        "home_current_set_games": 0,
                        "away_current_set_games": 0,
                        "home_total_games": 6,
                        "away_total_games": 3,
                        "set_scores": ((6, 3), (0, 0)),
                    },
                },
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.price == Decimal("0.995")
    assert decision.metadata["sports_tail_reason"] == "tennis_set_winner_locked"
    assert decision.metadata["sports_tail_maker_bid_reason"] == "ask_above_locked_price_cap"
    assert decision.order_type == OrderType.GTC
    assert decision.post_only is True


def test_tennis_completed_set_winner_places_maker_bid_when_best_ask_is_missing() -> None:
    now = datetime(2026, 4, 29, 9, 4, tzinfo=timezone.utc)
    market = Market(
        condition_id="tennis-first-set-missing-ask-condition",
        market_slug="atp-gaston-blanc-2026-04-29-first-set-winner-Gaston-vs-Blanch",
        market_question="Hugo Gaston vs Darwin Blanch first set winner",
        event_title="Mauthausen: Hugo Gaston vs Darwin Blanch",
        event_slug="atp-gaston-blanc-2026-04-29",
        end_date=datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="gaston-first-set", outcome="Gaston"),
            MarketOutcome(token_id="blanch-first-set", outcome="Blanch"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    ).with_tick_size(Decimal("0.001"))

    decision = decide_entry(
        CurrentStrategyConfig(sports_min_liquidity_usdc=Decimal("0.01")),
        ExtensionContext(
            trace_id="trace-tennis-first-set-missing-ask",
            market=market,
            token_id="gaston-first-set",
            orderbook=OrderbookSnapshot(
                token_id="gaston-first-set",
                best_bid=Decimal("0.001"),
                best_ask=None,
                bids=(PriceLevel(price=Decimal("0.001"), size=Decimal("20")),),
                asks=(),
                received_at=now,
                condition_id="tennis-first-set-missing-ask-condition",
            ),
            amount_usdc=Decimal("5"),
            now=now,
            metadata={
                "sports_tail_entry_signal_reason": "live_outcome_lock_candidate",
                "sports_tail_game": {
                    "league": "Mauthausen, Austria",
                    "home_name": "Hugo Gaston",
                    "away_name": "Darwin Blanch",
                    "home_score": 1,
                    "away_score": 0,
                    "period": "S2",
                    "status": "live",
                    "observed_at": "2026-04-29T09:04:00+00:00",
                    "tennis_state": {
                        "home_sets_won": 1,
                        "away_sets_won": 0,
                        "current_set": 2,
                        "home_current_set_games": 2,
                        "away_current_set_games": 3,
                        "home_total_games": 8,
                        "away_total_games": 6,
                        "set_scores": ((6, 3), (2, 3)),
                    },
                },
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.price == Decimal("0.995")
    assert decision.metadata["sports_tail_reason"] == "tennis_set_winner_locked"
    assert decision.metadata["sports_tail_maker_bid_reason"] == "missing_best_ask_locked_outcome"
    assert decision.order_type == OrderType.GTC
    assert decision.post_only is True


def test_entry_plan_allocates_locked_set_winner_maker_bid_without_ask_depth_at_price_cap() -> None:
    now = datetime(2026, 4, 29, 8, 50, tzinfo=timezone.utc)
    market = Market(
        condition_id="tennis-first-set-plan-condition",
        market_slug="atp-gaston-blanc-2026-04-29-first-set-winner-Gaston-vs-Blanch",
        market_question="Hugo Gaston vs Darwin Blanch first set winner",
        event_title="Mauthausen: Hugo Gaston vs Darwin Blanch",
        event_slug="atp-gaston-blanc-2026-04-29",
        end_date=datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="gaston-first-set", outcome="Gaston"),
            MarketOutcome(token_id="blanch-first-set", outcome="Blanch"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    ).with_tick_size(Decimal("0.001"))
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(
            config=CurrentStrategyConfig(
                sports_min_liquidity_usdc=Decimal("0.01"),
                sports_min_expected_profit_usdc=Decimal("0.001"),
                sports_profit_take_min_profit_usdc=Decimal("0.001"),
            ),
        ).hooks,
    )

    plan = service.build_entry_plan(
        market=market,
        token_id="gaston-first-set",
        orderbook=OrderbookSnapshot(
            token_id="gaston-first-set",
            best_bid=Decimal("0.001"),
            best_ask=Decimal("1"),
            bids=(PriceLevel(price=Decimal("0.001"), size=Decimal("20")),),
            asks=(PriceLevel(price=Decimal("1"), size=Decimal("20")),),
            received_at=now,
            condition_id="tennis-first-set-plan-condition",
        ),
        trace_id="trace-tennis-first-set-plan",
        portfolio_budget_usdc=Decimal("10"),
        available_usdc=Decimal("10"),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("10"),
        max_total_usdc=Decimal("10"),
        metadata={
            "sports_tail_entry_signal_reason": "live_outcome_lock_candidate",
            "sports_tail_game": {
                "league": "Mauthausen, Austria",
                "home_name": "Hugo Gaston",
                "away_name": "Darwin Blanch",
                "home_score": 1,
                "away_score": 0,
                "period": "S2",
                "status": "live",
                "observed_at": "2026-04-29T08:50:00+00:00",
                "tennis_state": {
                    "home_sets_won": 1,
                    "away_sets_won": 0,
                    "current_set": 2,
                    "home_current_set_games": 0,
                    "away_current_set_games": 0,
                    "home_total_games": 6,
                    "away_total_games": 3,
                    "set_scores": ((6, 3), (0, 0)),
                },
            },
        },
    )

    assert plan.ready_to_trade is True
    assert plan.intent is not None
    assert plan.intent.price == Decimal("0.995")
    assert plan.intent.order_type == OrderType.GTC
    assert plan.intent.post_only is True
    assert plan.metadata["sports_tail_maker_bid_reason"] == "ask_above_locked_price_cap"


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


def test_entry_plan_allows_scale_in_without_exit_order_in_settlement_mode_when_advantage_strengthens() -> None:
    market = _moneyline_market()
    position = Position(
        condition_id=market.condition_id,
        token_id="home",
        shares=Decimal("12"),
        cost_usdc=Decimal("12"),
        market_slug=market.market_slug,
    )
    account_snapshot = AccountSnapshot(
        balance_usdc=Decimal("50"),
        allowance_usdc=Decimal("50"),
        allow_new_entries=True,
        positions=(position,),
        fills=(
            Fill(
                trace_id="trace-initial-buy",
                condition_id=market.condition_id,
                token_id="home",
                side="BUY",
                notional_usdc=Decimal("12"),
                confirmed_at=datetime(2026, 4, 27, 0, 1, tzinfo=timezone.utc),
            ),
        ),
    )
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(
            config=CurrentStrategyConfig(sports_max_event_exposure_usdc=Decimal("40"))
        ).hooks,
    )

    plan = service.build_entry_plan(
        market=market,
        orderbook=_orderbook(token_id="home", best_ask=Decimal("0.94")),
        account_snapshot=account_snapshot,
        token_id="home",
        trace_id="trace-scale-in-plan",
        portfolio_budget_usdc=Decimal("20"),
        available_usdc=Decimal("50"),
        max_order_usdc=Decimal("20"),
        max_market_usdc=Decimal("40"),
        max_total_usdc=Decimal("60"),
        metadata={
            "sports_tail_game": {
                "league": "NBA",
                "home_name": "NYK",
                "away_name": "BOS",
                "home_score": 112,
                "away_score": 100,
                "period": "Q4",
                "seconds_remaining": 80,
                "status": "live",
                "observed_at": "2026-04-27T00:00:00+00:00",
            },
        },
    )

    assert plan.ready_to_trade is True
    assert plan.intent is not None
    assert plan.intent.token_id == "home"
    assert plan.intent.amount_usdc == Decimal("6")
    assert getattr(plan.intent, "allow_open_exit_overlap") is True
    assert plan.metadata is not None
    assert plan.metadata["sports_tail_reason"] == "scale_in_moneyline_advantage"
    assert plan.metadata["sports_tail_opportunity_type"] == "scale_in_advantage"


def test_entry_plan_uses_tennis_total_games_when_scaling_in_totals() -> None:
    market = _tennis_totals_market()
    position = Position(
        condition_id=market.condition_id,
        token_id="tennis-over",
        shares=Decimal("12"),
        cost_usdc=Decimal("12"),
        market_slug=market.market_slug,
        open_sell_shares=Decimal("12"),
    )
    exit_order = Order(
        trace_id="trace-tennis-exit-scale",
        condition_id=market.condition_id,
        token_id="tennis-over",
        market_slug=market.market_slug,
        side=OrderSide.SELL,
        order_type=OrderType.GTC,
        price=Decimal("0.99"),
        size_shares=Decimal("12"),
        remaining_shares=Decimal("12"),
        status=OrderStatus.LIVE,
        order_id="tennis-exit-scale-order",
    )
    account_snapshot = AccountSnapshot(
        balance_usdc=Decimal("50"),
        allowance_usdc=Decimal("50"),
        allow_new_entries=True,
        positions=(position,),
        open_orders=(exit_order,),
        fills=(
            Fill(
                trace_id="trace-tennis-initial-buy",
                condition_id=market.condition_id,
                token_id="tennis-over",
                side="BUY",
                notional_usdc=Decimal("12"),
                confirmed_at=datetime(2026, 4, 27, 0, 1, tzinfo=timezone.utc),
            ),
        ),
    )
    service = TradingDecisionService(
        extension_hooks=CurrentStrategy(
            config=CurrentStrategyConfig(sports_max_event_exposure_usdc=Decimal("40"))
        ).hooks,
    )

    plan = service.build_entry_plan(
        market=market,
        orderbook=_orderbook(token_id="tennis-over", best_ask=Decimal("0.94")),
        account_snapshot=account_snapshot,
        token_id="tennis-over",
        trace_id="trace-tennis-scale-in-plan",
        portfolio_budget_usdc=Decimal("20"),
        available_usdc=Decimal("50"),
        max_order_usdc=Decimal("20"),
        max_market_usdc=Decimal("40"),
        max_total_usdc=Decimal("60"),
        metadata={
            "sports_tail_game": {
                "league": "WTA",
                "home_name": "Rada Zolotareva",
                "away_name": "Despina Papamichail",
                "home_score": 0,
                "away_score": 0,
                "period": "S2",
                "status": "live",
                "observed_at": "2026-04-27T00:00:00+00:00",
                "tennis_state": {
                    "home_sets_won": 1,
                    "away_sets_won": 0,
                    "current_set": 2,
                    "home_current_set_games": 6,
                    "away_current_set_games": 5,
                    "home_total_games": 12,
                    "away_total_games": 11,
                    "set_scores": ((6, 4), (6, 5)),
                },
            }
        },
    )

    assert plan.ready_to_trade is True
    assert plan.intent is not None
    assert plan.intent.token_id == "tennis-over"
    assert plan.intent.amount_usdc == Decimal("6")
    assert getattr(plan.intent, "allow_open_exit_overlap") is True
    assert plan.metadata is not None
    assert plan.metadata["sports_tail_reason"] == "scale_in_tennis_totals_over_advantage"
    assert plan.metadata["sports_tail_opportunity_type"] == "scale_in_advantage"


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


def test_admin_live_source_gap_diagnostics_groups_tracked_markets_without_live_state() -> None:
    result = asyncio.run(_run_admin_live_source_gap_diagnostics_flow())

    assert result["total_tracked_markets"] == 4
    assert result["tracked_markets"] == 3
    assert result["live_state_markets"] == 1
    assert result["missing_live_state_markets"] == 1
    assert result["deferred_future_schedule_markets"] == 1
    assert result["by_urgency"] == [
        {"urgency": "started_or_past_due", "count": 1},
    ]
    assert result["by_prefix"] == [
        {"prefix": "mlb", "count": 1},
    ]
    assert [item["market_slug"] for item in result["items"]] == [
        "mlb-test-gap-2026-05-01",
    ]
    assert result["items"][0]["gap_urgency"] == "started_or_past_due"


def test_admin_confirmation_refuses_non_confirmable_candidate() -> None:
    result = asyncio.run(_run_admin_auto_candidate_confirmation_attempt())

    assert result["candidates"]["total"] == 1
    candidate = result["candidates"]["items"][0]
    assert candidate["action"] == "auto_execute"
    assert candidate["confirmable"] is False
    assert result["confirmation"]["status"] == "failed"
    assert result["confirmation"]["reason"] == "candidate_not_confirmable"
    assert result["confirmation"]["candidate"]["confirmable"] is False


def test_admin_candidate_projects_blocked_auto_execute_as_rejected() -> None:
    market = _moneyline_market()
    plan = EntryPlan(
        trace_id="trace-blocked-auto",
        market=market,
        orderbook=None,
        allocation_plan=AllocationPlan(trace_id="trace-blocked-auto", total_budget_usdc=Decimal("10")),
        allocation=Allocation(
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            token_id="home",
            target_budget_usdc=Decimal("5"),
            buy_budget_usdc=Decimal("0"),
            reason="below_min_order_size",
        ),
        intent=None,
        reason="",
        metadata={
            "sports_tail_action": "auto_execute",
            "sports_tail_reason": "tennis_set_winner_locked",
            "sports_execution_permission": "auto_execute",
        },
    )

    candidate = AdminService()._candidate_payload(market, "home", plan)

    assert candidate["ready_to_trade"] is False
    assert candidate["action"] == "reject"
    assert candidate["strategy_action"] == "auto_execute"
    assert candidate["accepted"] is False
    assert candidate["confirmable"] is False
    assert candidate["reason"] == "below_min_order_size"
    assert candidate["payload"]["sports_tail_action"] == "auto_execute"


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


def test_live_market_more_than_one_hour_from_close_is_not_tail_candidate() -> None:
    now = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
    market = _moneyline_market().with_metadata(end_date=datetime(2026, 4, 27, 2, 1, tzinfo=timezone.utc))
    orderbook = _orderbook(token_id="home", best_ask=Decimal("0.96"))

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-live-far-close",
            market=market,
            token_id="home",
            orderbook=orderbook,
            amount_usdc=Decimal("10"),
            now=now,
            metadata={
                "sports_tail_game": {
                    "league": "NBA",
                    "home_name": "NYK",
                    "away_name": "BOS",
                    "home_score": 96,
                    "away_score": 94,
                    "period": "Q4",
                    "seconds_remaining": 600,
                    "status": "live",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "market_end_too_far"
    assert decision.metadata["sports_tail_reason"] == "market_end_too_far"


def test_live_mlb_uses_baseball_state_not_gamma_settlement_end_date_for_tail_gate() -> None:
    now = datetime(2026, 4, 30, 0, 42, tzinfo=timezone.utc)
    market = Market(
        condition_id="mlb-live-condition",
        market_slug="mlb-stl-pit-2026-04-29",
        market_question="St. Louis Cardinals vs. Pittsburgh Pirates",
        event_title="St. Louis Cardinals vs. Pittsburgh Pirates",
        event_slug="mlb-stl-pit-2026-04-29",
        category="Sports",
        tags=("MLB",),
        outcomes=(
            MarketOutcome(token_id="cardinals", outcome="St. Louis Cardinals"),
            MarketOutcome(token_id="pirates", outcome="Pittsburgh Pirates"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    ).with_metadata(end_date=datetime(2026, 5, 6, 22, 40, tzinfo=timezone.utc))

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-live-mlb-settlement-end-date",
            market=market,
            token_id="cardinals",
            orderbook=_orderbook(token_id="cardinals", best_ask=Decimal("0.94")),
            amount_usdc=Decimal("10"),
            now=now,
            metadata={
                "sports_tail_game": {
                    "league": "MLB",
                    "home_name": "Pirates",
                    "away_name": "Cardinals",
                    "home_score": 1,
                    "away_score": 9,
                    "period": "T7",
                    "seconds_remaining": None,
                    "status": "live",
                    "observed_at": "2026-04-30T00:42:00+00:00",
                    "baseball_state": {
                        "current_inning": 7,
                        "inning_half": "top",
                        "outs": 2,
                        "offense_team": "St. Louis Cardinals",
                        "defense_team": "Pittsburgh Pirates",
                        "occupied_bases": (2,),
                    },
                },
                "sports_live_match": {
                    "matched_home_alias": "Pittsburgh Pirates",
                    "matched_away_alias": "St. Louis Cardinals",
                },
            },
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "baseball_not_late_enough"
    assert decision.metadata["sports_tail_reason"] == "baseball_not_late_enough"


def test_mlb_structured_tail_state_allows_official_source_age_above_generic_limit() -> None:
    now = datetime(2026, 4, 30, 1, 40, 55, tzinfo=timezone.utc)
    market = Market(
        condition_id="mlb-freshness-condition",
        market_slug="mlb-stl-pit-2026-04-29",
        market_question="St. Louis Cardinals vs. Pittsburgh Pirates",
        event_title="St. Louis Cardinals vs. Pittsburgh Pirates",
        event_slug="mlb-stl-pit-2026-04-29",
        category="Sports",
        tags=("MLB",),
        outcomes=(
            MarketOutcome(token_id="cardinals", outcome="St. Louis Cardinals"),
            MarketOutcome(token_id="pirates", outcome="Pittsburgh Pirates"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-mlb-official-source-age",
            market=market,
            token_id="cardinals",
            orderbook=_orderbook(token_id="cardinals", best_ask=Decimal("0.94")),
            amount_usdc=Decimal("10"),
            now=now,
            metadata={
                "sports_tail_game": {
                    "league": "MLB",
                    "home_name": "Pirates",
                    "away_name": "Cardinals",
                    "home_score": 3,
                    "away_score": 9,
                    "period": "B9",
                    "seconds_remaining": None,
                    "status": "live",
                    "observed_at": "2026-04-30T01:40:30+00:00",
                    "source": "mlb",
                    "baseball_state": {
                        "current_inning": 9,
                        "inning_half": "bottom",
                        "outs": 2,
                        "offense_team": "Pittsburgh Pirates",
                        "defense_team": "St. Louis Cardinals",
                        "occupied_bases": (),
                    },
                },
                "sports_live_match": {
                    "matched_home_alias": "Pittsburgh Pirates",
                    "matched_away_alias": "St. Louis Cardinals",
                },
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.metadata["sports_tail_reason"] == "mlb_moneyline_late_lead"


def test_mlb_moneyline_eighth_inning_leader_can_enter_when_no_scoring_threat() -> None:
    now = datetime(2026, 4, 30, 2, 5, 55, tzinfo=timezone.utc)
    market = Market(
        condition_id="mlb-eighth-leader-condition",
        market_slug="mlb-ari-mil-2026-04-29",
        market_question="Arizona Diamondbacks vs. Milwaukee Brewers",
        event_title="Arizona Diamondbacks vs. Milwaukee Brewers",
        event_slug="mlb-ari-mil-2026-04-29",
        category="Sports",
        tags=("MLB",),
        outcomes=(
            MarketOutcome(token_id="diamondbacks", outcome="Arizona Diamondbacks"),
            MarketOutcome(token_id="brewers", outcome="Milwaukee Brewers"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = decide_entry(
        CurrentStrategyConfig(
            sports_min_liquidity_usdc=Decimal("0.01"),
            sports_min_expected_profit_usdc=Decimal("0.03"),
            sports_min_expected_profit_per_hour_usdc=Decimal("0.10"),
        ),
        ExtensionContext(
            trace_id="trace-mlb-eighth-leader",
            market=market,
            token_id="diamondbacks",
            orderbook=_orderbook(token_id="diamondbacks", best_ask=Decimal("0.89")),
            amount_usdc=Decimal("10"),
            now=now,
            metadata={
                "sports_tail_game": {
                    "league": "MLB",
                    "home_name": "Brewers",
                    "away_name": "Diamondbacks",
                    "home_score": 2,
                    "away_score": 4,
                    "period": "T8",
                    "seconds_remaining": None,
                    "status": "live",
                    "observed_at": "2026-04-30T02:05:55+00:00",
                    "source": "mlb",
                    "baseball_state": {
                        "current_inning": 8,
                        "inning_half": "top",
                        "outs": 1,
                        "offense_team": "Arizona Diamondbacks",
                        "defense_team": "Milwaukee Brewers",
                        "occupied_bases": (1,),
                    },
                },
                "sports_live_match": {
                    "matched_home_alias": "Milwaukee Brewers",
                    "matched_away_alias": "Arizona Diamondbacks",
                },
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.metadata["sports_tail_reason"] == "mlb_moneyline_eighth_lead"


def test_mlb_moneyline_eighth_inning_rejects_scoring_position_threat() -> None:
    now = datetime(2026, 4, 30, 2, 5, 55, tzinfo=timezone.utc)
    market = Market(
        condition_id="mlb-eighth-threat-condition",
        market_slug="mlb-ari-mil-2026-04-29",
        market_question="Arizona Diamondbacks vs. Milwaukee Brewers",
        event_title="Arizona Diamondbacks vs. Milwaukee Brewers",
        event_slug="mlb-ari-mil-2026-04-29",
        category="Sports",
        tags=("MLB",),
        outcomes=(
            MarketOutcome(token_id="diamondbacks", outcome="Arizona Diamondbacks"),
            MarketOutcome(token_id="brewers", outcome="Milwaukee Brewers"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )

    decision = decide_entry(
        CurrentStrategyConfig(sports_min_liquidity_usdc=Decimal("0.01")),
        ExtensionContext(
            trace_id="trace-mlb-eighth-threat",
            market=market,
            token_id="diamondbacks",
            orderbook=_orderbook(token_id="diamondbacks", best_ask=Decimal("0.89")),
            amount_usdc=Decimal("10"),
            now=now,
            metadata={
                "sports_tail_game": {
                    "league": "MLB",
                    "home_name": "Brewers",
                    "away_name": "Diamondbacks",
                    "home_score": 2,
                    "away_score": 4,
                    "period": "T8",
                    "seconds_remaining": None,
                    "status": "live",
                    "observed_at": "2026-04-30T02:05:55+00:00",
                    "source": "mlb",
                    "baseball_state": {
                        "current_inning": 8,
                        "inning_half": "top",
                        "outs": 1,
                        "offense_team": "Arizona Diamondbacks",
                        "defense_team": "Milwaukee Brewers",
                        "occupied_bases": (2,),
                    },
                },
                "sports_live_match": {
                    "matched_home_alias": "Milwaukee Brewers",
                    "matched_away_alias": "Arizona Diamondbacks",
                },
            },
        ),
    )

    assert decision.action.value == "skip"
    assert decision.reason == "baseball_threat_on_base"


def test_totals_over_locked_bypasses_far_market_end_window() -> None:
    now = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
    market = _totals_market().with_metadata(end_date=datetime(2026, 4, 27, 2, 1, tzinfo=timezone.utc))
    orderbook = _orderbook(token_id="over", best_ask=Decimal("0.98"))

    decision = decide_entry(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-live-far-close-locked-over",
            market=market,
            token_id="over",
            orderbook=orderbook,
            amount_usdc=Decimal("10"),
            now=now,
            metadata={
                "sports_tail_game": {
                    "league": "NHL",
                    "home_name": "TB",
                    "away_name": "MON",
                    "home_score": 3,
                    "away_score": 2,
                    "period": "P3",
                    "seconds_remaining": 900,
                    "status": "live",
                    "observed_at": "2026-04-27T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.action.value == "buy"
    assert decision.metadata["sports_tail_reason"] == "totals_over_locked"


def test_allocation_reports_far_close_before_missing_best_ask() -> None:
    now = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
    market = _moneyline_market().with_metadata(end_date=datetime(2026, 4, 27, 2, 1, tzinfo=timezone.utc))
    service = TradingDecisionService(extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks)

    plan = service.build_entry_plan(
        market=market,
        orderbook=OrderbookSnapshot(
            token_id="home",
            best_bid=None,
            best_ask=None,
            bids=(),
            asks=(),
            received_at=now,
            condition_id=market.condition_id,
            market_slug=market.market_slug,
        ),
        token_id="home",
        trace_id="trace-far-close-no-ask",
        portfolio_budget_usdc=Decimal("10"),
        available_usdc=Decimal("10"),
        max_order_usdc=Decimal("10"),
        max_market_usdc=Decimal("20"),
        max_total_usdc=Decimal("20"),
        metadata={
            "sports_tail_game": {
                "league": "NBA",
                "home_name": "NYK",
                "away_name": "BOS",
                "home_score": 96,
                "away_score": 94,
                "period": "Q4",
                "seconds_remaining": 600,
                "status": "live",
                "observed_at": "2026-04-27T00:00:00+00:00",
            }
        },
    )

    assert plan.intent is None
    assert plan.allocation is not None
    assert plan.allocation.reason == "market_end_too_far"
    assert plan.metadata["sports_tail_reason"] == "market_end_too_far"


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


def test_follow_up_sell_is_not_created_after_buy_fill() -> None:
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

    assert decisions == ()


def test_profit_take_follow_up_sell_is_created_after_tagged_buy_fill() -> None:
    market = _totals_market().with_tick_size(Decimal("0.01"))
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    decisions = strategy.decide_follow_up(
        ExtensionContext(
            trace_id="trace-profit-take-follow-up",
            market=market,
            token_id="over",
            order_result=OrderResult(
                trace_id="trace-profit-take-follow-up",
                condition_id=market.condition_id,
                token_id="over",
                status=OrderResultStatus.FULL_FILL,
                intent=BuyOrderIntent(
                    trace_id="trace-profit-take-follow-up",
                    condition_id=market.condition_id,
                    token_id="over",
                    price=Decimal("0.99"),
                    amount_usdc=Decimal("5"),
                    market_slug=market.market_slug,
                    metadata={
                        "sports_exit_mode": "profit_take",
                        "sports_profit_take_target_price": "0.999",
                    },
                ),
                market_slug=market.market_slug,
                side=OrderSide.BUY,
                price=Decimal("0.99"),
                matched_shares=Decimal("5.050505050505050505"),
                spent_usdc=Decimal("5"),
            ),
        )
    )

    assert len(decisions) == 1
    assert decisions[0].action.value == "sell"
    assert decisions[0].reason == "strategy_profit_take"
    assert decisions[0].price == Decimal("0.99")
    assert decisions[0].size_shares == Decimal("5.050505050505050505")
    assert decisions[0].metadata["sports_exit_mode"] == "profit_take"


def test_profit_take_overlay_follow_up_sell_is_created_after_settlement_buy_fill() -> None:
    market = _moneyline_market().with_tick_size(Decimal("0.01"))
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    decisions = strategy.decide_follow_up(
        ExtensionContext(
            trace_id="trace-overlay-follow-up",
            market=market,
            token_id="home",
            order_result=OrderResult(
                trace_id="trace-overlay-follow-up",
                condition_id=market.condition_id,
                token_id="home",
                status=OrderResultStatus.FULL_FILL,
                intent=BuyOrderIntent(
                    trace_id="trace-overlay-follow-up",
                    condition_id=market.condition_id,
                    token_id="home",
                    price=Decimal("0.93"),
                    amount_usdc=Decimal("5"),
                    market_slug=market.market_slug,
                    metadata={
                        "sports_exit_mode": "settlement",
                        "sports_profit_take_overlay_enabled": True,
                        "sports_profit_take_target_price": "0.94",
                    },
                ),
                market_slug=market.market_slug,
                side=OrderSide.BUY,
                price=Decimal("0.93"),
                matched_shares=Decimal("5.376342"),
                spent_usdc=Decimal("4.99999806"),
            ),
        )
    )

    assert len(decisions) == 1
    assert decisions[0].action.value == "sell"
    assert decisions[0].reason == "strategy_profit_take"
    assert decisions[0].price == Decimal("0.94")
    assert decisions[0].size_shares == Decimal("5.376342")
    assert decisions[0].metadata["sports_exit_mode"] == "settlement"
    assert decisions[0].metadata["sports_profit_take_overlay_enabled"] is True


def test_position_exit_waits_for_settlement_by_default() -> None:
    market = _totals_market()
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    decision = strategy.decide_exit(
        ExtensionContext(
            trace_id="trace-position-exit",
            market=market,
            token_id="over",
            position=Position(
                condition_id=market.condition_id,
                token_id="over",
                shares=Decimal("3"),
                cost_usdc=Decimal("2.97"),
                market_slug=market.market_slug,
            ),
            metadata={"exit_trigger": "position_updated"},
        )
    )

    assert decision.action.value == "skip"
    assert decision.reason == "settlement_only_exit_disabled"


def test_recovery_keeps_ended_single_game_open_for_ended_not_closed_scan() -> None:
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
                    "observed_at": "2026-04-27T00:00:00+00:00",
                }
            },
        ),
    )

    assert decision.pause_trading is False
    assert decision.pause_reason == ""


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
                    "home_score": 0,
                    "away_score": 0,
                    "period": "Postponed",
                    "seconds_remaining": None,
                    "status": "postponed",
                    "observed_at": "2026-04-27T00:59:55+00:00",
                }
            },
        ),
    )

    assert decision.pause_trading is True
    assert decision.pause_reason == "sports_live_state_postponed"


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
    ]


def test_recovery_does_not_create_exit_order_in_settlement_only_mode() -> None:
    market = _moneyline_market()
    position = Position(
        condition_id=market.condition_id,
        token_id="away",
        shares=Decimal("4"),
        cost_usdc=Decimal("3.5"),
        market_slug=market.market_slug,
    )

    decision = decide_recovery(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-settlement-only-recovery",
            market=market,
            position=position,
        ),
    )

    assert decision.reason == "strategy_recovery"
    assert decision.actions == ()


def test_recovery_places_profit_take_for_near_settlement_position_missing_overlay() -> None:
    market = _moneyline_market().with_tick_size(Decimal("0.01"))
    position = Position(
        condition_id=market.condition_id,
        token_id="home",
        shares=Decimal("5.376342"),
        cost_usdc=Decimal("4.99999806"),
        market_slug=market.market_slug,
    )

    decision = decide_recovery(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-near-settlement-overlay-recovery",
            market=market,
            position=position,
        ),
    )

    assert decision.reason == "strategy_recovery"
    assert len(decision.actions) == 1
    assert decision.actions[0].action.value == "sell"
    assert decision.actions[0].reason == "recovery_profit_take"
    assert decision.actions[0].price == Decimal("0.94")
    assert decision.actions[0].size_shares == Decimal("5.376342")
    assert decision.actions[0].metadata["sports_profit_take_expected_profit_usdc"] == "0.05376342"


def test_recovery_places_profit_take_for_high_price_uncovered_position() -> None:
    market = _totals_market().with_tick_size(Decimal("0.01"))
    position = Position(
        condition_id=market.condition_id,
        token_id="over",
        shares=Decimal("5.0505"),
        cost_usdc=Decimal("4.94"),
        market_slug=market.market_slug,
    )

    decision = decide_recovery(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-recovery-profit-take",
            market=market,
            position=position,
        ),
    )

    assert decision.reason == "strategy_recovery"
    assert len(decision.actions) == 1
    assert decision.actions[0].action.value == "sell"
    assert decision.actions[0].reason == "recovery_profit_take"
    assert decision.actions[0].token_id == "over"
    assert decision.actions[0].price == Decimal("0.99")
    assert decision.actions[0].size_shares == Decimal("5.0505")
    assert decision.actions[0].metadata["sports_exit_mode"] == "profit_take"
    assert decision.actions[0].metadata["sports_profit_take_expected_profit_usdc"] == "0.059995"


def test_recovery_uses_profitable_best_bid_when_one_tick_profit_is_too_small() -> None:
    market = _totals_market().with_tick_size(Decimal("0.001"))
    position = Position(
        condition_id=market.condition_id,
        token_id="over",
        shares=Decimal("5.0505"),
        cost_usdc=Decimal("4.9999"),
        market_slug=market.market_slug,
    )
    orderbook = OrderbookSnapshot(
        token_id="over",
        condition_id=market.condition_id,
        market_slug=market.market_slug,
        best_bid=Decimal("0.999"),
        best_ask=None,
        bids=(PriceLevel(price=Decimal("0.999"), size=Decimal("100")),),
        asks=(),
        received_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        tick_size=Decimal("0.001"),
    )

    decision = decide_recovery(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-recovery-profit-take-best-bid",
            market=market,
            position=position,
            market_token_views=(
                MarketTokenView(token_id="over", outcome="Over", orderbook=orderbook),
            ),
        ),
    )

    assert decision.reason == "strategy_recovery"
    assert len(decision.actions) == 1
    assert decision.actions[0].reason == "recovery_profit_take"
    assert decision.actions[0].price == Decimal("0.999")
    assert decision.actions[0].metadata["sports_profit_take_price_source"] == "best_bid"
    assert decision.actions[0].metadata["sports_profit_take_expected_profit_usdc"] == "0.0455495"


def test_recovery_places_profit_take_for_unknown_legacy_high_price_position() -> None:
    market = _unknown_legacy_market().with_tick_size(Decimal("0.01"))
    position = Position(
        condition_id=market.condition_id,
        token_id="legacy",
        shares=Decimal("5.0505"),
        cost_usdc=Decimal("4.94"),
        market_slug=market.market_slug,
    )

    decision = decide_recovery(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-legacy-recovery-profit-take",
            market=market,
            position=position,
        ),
    )

    assert decision.pause_trading is True
    assert decision.pause_reason == "missing_sports_target"
    assert len(decision.actions) == 1
    assert decision.actions[0].action.value == "sell"
    assert decision.actions[0].reason == "recovery_profit_take"
    assert decision.actions[0].token_id == "legacy"
    assert decision.actions[0].price == Decimal("0.99")
    assert decision.actions[0].size_shares == Decimal("5.0505")


def test_recovery_does_not_repeat_profit_take_for_unknown_candidate_without_orderbook() -> None:
    market = _unknown_legacy_market().with_trading_status(TradingStatus.CANDIDATE).with_tick_size(Decimal("0.01"))
    position = Position(
        condition_id=market.condition_id,
        token_id="legacy",
        shares=Decimal("5.0505"),
        cost_usdc=Decimal("4.9999"),
        market_slug=market.market_slug,
    )

    decision = decide_recovery(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-legacy-candidate-no-orderbook",
            market=market,
            position=position,
        ),
    )

    assert decision.pause_trading is True
    assert decision.pause_reason == "missing_sports_target"
    assert decision.actions == ()


def test_recovery_does_not_profit_take_unknown_legacy_low_price_position() -> None:
    market = _unknown_legacy_market().with_tick_size(Decimal("0.01"))
    position = Position(
        condition_id=market.condition_id,
        token_id="legacy",
        shares=Decimal("103"),
        cost_usdc=Decimal("1.03"),
        market_slug=market.market_slug,
    )

    decision = decide_recovery(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-legacy-low-price-recovery",
            market=market,
            position=position,
        ),
    )

    assert decision.pause_trading is True
    assert decision.pause_reason == "missing_sports_target"
    assert decision.actions == ()


def test_recovery_keeps_existing_profit_take_exit_order_in_settlement_only_mode() -> None:
    market = _totals_market().with_tick_size(Decimal("0.01"))
    open_sell = Order(
        condition_id=market.condition_id,
        token_id="over",
        side=OrderSide.SELL,
        order_type=OrderType.GTC,
        price=Decimal("1.00"),
        size_shares=Decimal("5.0505"),
        remaining_shares=Decimal("5.0505"),
        status=OrderStatus.LIVE,
        order_id="sell-profit-take",
        market_slug=market.market_slug,
        reason="recovery_profit_take",
    )

    decision = decide_recovery(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-keep-profit-take-open-exit",
            market=market,
            open_orders=(open_sell,),
        ),
    )

    assert decision.reason == "strategy_recovery"
    assert decision.actions == ()


def test_recovery_cancels_historical_open_exit_order_in_settlement_only_mode() -> None:
    market = _moneyline_market()
    open_sell = Order(
        condition_id=market.condition_id,
        token_id="away",
        side=OrderSide.SELL,
        order_type=OrderType.GTC,
        price=Decimal("0.99"),
        size_shares=Decimal("4"),
        remaining_shares=Decimal("4"),
        status=OrderStatus.LIVE,
        order_id="sell-1",
        market_slug=market.market_slug,
    )

    decision = decide_recovery(
        CurrentStrategyConfig(),
        ExtensionContext(
            trace_id="trace-settlement-only-open-exit",
            market=market,
            open_orders=(open_sell,),
        ),
    )

    assert [(action.action.value, action.reason, action.order_id) for action in decision.actions] == [
        ("cancel", "settlement_only_open_exit_order_detected", "sell-1"),
    ]


def test_recovery_keeps_fresh_open_entry_order_within_strategy_ttl() -> None:
    now = datetime(2026, 4, 30, 7, 40, 20, tzinfo=timezone.utc)
    market = _moneyline_market()
    open_buy = Order(
        condition_id=market.condition_id,
        token_id="away",
        side=OrderSide.BUY,
        order_type=OrderType.GTC,
        price=Decimal("0.995"),
        amount_usdc=Decimal("5"),
        remaining_shares=Decimal("5.02"),
        status=OrderStatus.LIVE,
        order_id="buy-1",
        market_slug=market.market_slug,
        created_at=now - timedelta(seconds=3),
    )

    decision = decide_recovery(
        CurrentStrategyConfig(sports_entry_maker_max_resting_seconds=10),
        ExtensionContext(
            trace_id="trace-fresh-open-entry",
            market=market,
            open_orders=(open_buy,),
            now=now,
        ),
    )

    assert decision.actions == ()


def test_recovery_cancels_stale_open_entry_order_after_strategy_ttl() -> None:
    now = datetime(2026, 4, 30, 7, 40, 20, tzinfo=timezone.utc)
    market = _moneyline_market()
    open_buy = Order(
        condition_id=market.condition_id,
        token_id="away",
        side=OrderSide.BUY,
        order_type=OrderType.GTC,
        price=Decimal("0.995"),
        amount_usdc=Decimal("5"),
        remaining_shares=Decimal("5.02"),
        status=OrderStatus.LIVE,
        order_id="buy-1",
        market_slug=market.market_slug,
        created_at=now - timedelta(seconds=11),
    )

    decision = decide_recovery(
        CurrentStrategyConfig(sports_entry_maker_max_resting_seconds=10),
        ExtensionContext(
            trace_id="trace-stale-open-entry",
            market=market,
            open_orders=(open_buy,),
            now=now,
        ),
    )

    assert [(action.action.value, action.reason, action.order_id) for action in decision.actions] == [
        ("cancel", "open_entry_order_detected", "buy-1"),
    ]


def test_recovery_can_cover_position_when_auto_exit_is_explicitly_enabled() -> None:
    market = _moneyline_market()
    position = Position(
        condition_id=market.condition_id,
        token_id="away",
        shares=Decimal("4"),
        cost_usdc=Decimal("3.5"),
        market_slug=market.market_slug,
    )

    decision = decide_recovery(
        CurrentStrategyConfig(auto_exit_enabled=True),
        ExtensionContext(
            trace_id="trace-auto-exit-recovery",
            market=market,
            position=position,
        ),
    )

    assert [(action.action.value, action.reason, action.token_id) for action in decision.actions] == [
        ("sell", "recovery_exit_shortage", "away"),
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


def _unknown_legacy_market() -> Market:
    return Market(
        condition_id="legacy-condition",
        market_slug="atp-ghibaud-pieri-2026-04-29-match-total-21pt5",
        market_question="",
        event_title="",
        category=None,
        tags=(),
        outcomes=(MarketOutcome(token_id="legacy", outcome=""),),
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
            "home_sets_won": 1,
            "away_sets_won": 0,
            "current_set": 2,
            "home_current_set_games": 5,
            "away_current_set_games": 3,
            "home_total_games": 11,
            "away_total_games": 6,
            "set_scores": ((6, 3), (5, 3)),
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


def _single_game_binary_prop_market() -> Market:
    return Market(
        condition_id="single-game-binary-prop-condition",
        market_slug="soccer-ars-che-2026-04-30-first-goal-yes-no",
        market_question="Arsenal vs Chelsea: Will there be a goal in the first 10 minutes?",
        event_title="Arsenal vs Chelsea",
        event_slug="soccer-ars-che-2026-04-30",
        category="Sports",
        tags=("Sports", "Soccer"),
        outcomes=(
            MarketOutcome(token_id="goal-yes", outcome="Yes"),
            MarketOutcome(token_id="goal-no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _league_winner_market() -> Market:
    return Market(
        condition_id="league-winner-condition",
        market_slug="2026-basketball-japan-b-league-winner-akita-northern-happinets",
        market_question="Will Akita Northern Happinets win Japan B League?",
        event_title="Japan B League: Winner",
        event_slug="2026-basketball-japan-b-league-winner",
        category="Sports",
        tags=("Sports", "Japan B League", "Basketball"),
        outcomes=(
            MarketOutcome(token_id="yes", outcome="Yes"),
            MarketOutcome(token_id="no", outcome="No"),
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


def _epl_top_goalscorer_yes_no_market() -> Market:
    return Market(
        condition_id="epl-top-goalscorer-condition",
        market_slug="will-player-l-be-the-top-goal-scorer-in-the-202526-english-premier-league-season",
        market_question="Will Player L be the top goal scorer in the 2025-26 English Premier League season?",
        event_title="English Premier League - Top Goalscorer",
        event_slug="english-premier-league-top-goalscorer",
        category="Sports",
        tags=("Premier League", "Soccer", "Sports"),
        outcomes=(
            MarketOutcome(token_id="epl-top-goalscorer-yes", outcome="Yes"),
            MarketOutcome(token_id="epl-top-goalscorer-no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _ucl_winner_yes_no_market() -> Market:
    return Market(
        condition_id="ucl-winner-condition",
        market_slug="will-bayern-munich-win-the-2025-26-uefa-womens-champions-league",
        market_question="Will Bayern Munich win the 2025-26 UEFA Women's Champions League?",
        event_title="UEFA Women's UCL: Winner",
        event_slug="uefa-womens-ucl-winner",
        category="Sports",
        tags=("Champions League", "Soccer", "Sports"),
        outcomes=(
            MarketOutcome(token_id="ucl-winner-yes", outcome="Yes"),
            MarketOutcome(token_id="ucl-winner-no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _nba_champion_yes_no_market() -> Market:
    return Market(
        condition_id="nba-champion-condition",
        market_slug="2026-nba-champion-oklahoma-city-thunder",
        market_question="Will the Oklahoma City Thunder win the 2026 NBA Championship?",
        event_title="2026 NBA Champion",
        event_slug="2026-nba-champion",
        category="Sports",
        tags=("NBA", "Basketball", "Sports"),
        outcomes=(
            MarketOutcome(token_id="nba-champion-yes", outcome="Yes"),
            MarketOutcome(token_id="nba-champion-no", outcome="No"),
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


def _tennis_first_set_total_market() -> Market:
    return Market(
        condition_id="tennis-first-set-total-condition",
        market_slug="wta-ren-hercog-2026-04-29-first-set-total-9pt5",
        market_question="Yufei Ren vs Polona Hercog first set total 9.5",
        event_title="Huzhou: Yufei Ren vs Polona Hercog",
        event_slug="wta-ren-hercog-2026-04-29",
        category="Sports",
        tags=("WTA", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="first-set-over", outcome="Over"),
            MarketOutcome(token_id="first-set-under", outcome="Under"),
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


async def _run_admin_live_source_gap_diagnostics_flow() -> dict[str, object]:
    registry = MarketRegistry()
    live_store = EntryMetadataStore()
    covered_market = _moneyline_market_for_index(0)
    missing_nba_market = _moneyline_market_for_index(1)
    outright_market = _league_winner_market()
    missing_mlb_market = Market(
        condition_id="mlb-gap-condition",
        market_slug="mlb-test-gap-2026-05-01",
        market_question="Test MLB live source gap",
        event_title="Test MLB Gap",
        event_slug="mlb-test-gap-2026-05-01",
        category="Sports",
        tags=("MLB",),
        outcomes=(
            MarketOutcome(token_id="mlb-home", outcome="Home"),
            MarketOutcome(token_id="mlb-away", outcome="Away"),
        ),
        game_start_time=datetime(2026, 4, 30, 2, 0, tzinfo=timezone.utc),
        trading_status=TradingStatus.ELIGIBLE,
    )
    missing_nba_market = missing_nba_market.with_metadata(
        game_start_time=datetime(2026, 5, 10, 2, 0, tzinfo=timezone.utc),
    )
    for market in (covered_market, missing_nba_market, missing_mlb_market, outright_market):
        registry.upsert(market)
    live_store.upsert(
        condition_id=covered_market.condition_id,
        source="unit_test",
        metadata={"sports_tail_game": _moneyline_live_game()},
    )
    live_store.upsert(
        condition_id=missing_nba_market.condition_id,
        source="unit_test",
        metadata={"unrelated_entry_metadata": True},
    )
    service = AdminService(
        runtime=SimpleNamespace(
            registry=registry,
            entry_metadata_store=live_store,
            extension=CurrentStrategy(config=CurrentStrategyConfig()),
        )
    )
    return await service.list_sports_live_source_gaps(limit=10, offset=0)


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
