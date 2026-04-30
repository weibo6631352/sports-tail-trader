from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.config import Settings
from strategies.current.sports_tail import (
    BaseballGameState,
    LiveGameState,
    LiveGameStatus,
    SportsMarketFamily,
    TailAction,
    SportsMarketSide,
    SportsMarketSnapshot,
    SportsMarketType,
    SportsTailPolicy,
    TennisGameState,
    evaluate_tail_opportunity,
)
from strategies.current.config import CurrentStrategyConfig


def test_sports_tail_rejects_live_source_conflict() -> None:
    game = LiveGameState(
        league="NBA",
        home_name="Orlando Magic",
        away_name="Detroit Pistons",
        home_score=90,
        away_score=82,
        period="Q4",
        status=LiveGameStatus.LIVE,
        seconds_remaining=60,
        source_conflicts=(
            {
                "source": "sofascore",
                "status": "paused",
                "raw_status": "Halftime",
            },
        ),
    )
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=SportsMarketSide.HOME,
        token_id="home",
        line=None,
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("10"),
    )

    result = evaluate_tail_opportunity(game, market, policy=SportsTailPolicy())

    assert result.accepted is False
    assert result.reason == "live_source_conflict"


def test_sports_tail_rejects_non_single_game_market_before_score_lock_logic() -> None:
    game = LiveGameState(
        league="NHL",
        home_name="Anaheim Ducks",
        away_name="Edmonton Oilers",
        home_score=1,
        away_score=4,
        period="P3",
        status=LiveGameStatus.LIVE,
        seconds_remaining=5,
    )
    market = SportsMarketSnapshot(
        market_family=SportsMarketFamily.SERIES,
        market_type=SportsMarketType.TOTALS,
        side=SportsMarketSide.OVER,
        token_id="series-over",
        line=Decimal("4.5"),
        best_ask=Decimal("0.98"),
        buyable_liquidity_usdc=Decimal("10"),
    )

    result = evaluate_tail_opportunity(game, market, policy=SportsTailPolicy())

    assert result.accepted is False
    assert result.reason == "series_market_not_auto_tradable"
    assert result.metadata["market_family"] == "series"


def test_mlb_moneyline_uses_baseball_state_instead_of_seconds_remaining() -> None:
    game = LiveGameState(
        league="MLB",
        home_name="Cleveland Guardians",
        away_name="Tampa Bay Rays",
        home_score=2,
        away_score=5,
        period="B9",
        status=LiveGameStatus.LIVE,
        seconds_remaining=None,
        baseball_state=BaseballGameState(
            current_inning=9,
            inning_half="bottom",
            outs=2,
            offense_team="Cleveland Guardians",
            defense_team="Tampa Bay Rays",
            occupied_bases=(),
        ),
    )
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=SportsMarketSide.AWAY,
        token_id="away",
        line=None,
        best_ask=Decimal("0.92"),
        buyable_liquidity_usdc=Decimal("10"),
    )

    result = evaluate_tail_opportunity(game, market, policy=SportsTailPolicy(min_moneyline_lead=3))

    assert result.accepted is True
    assert result.action == TailAction.AUTO_EXECUTE
    assert result.reason == "mlb_moneyline_late_lead"


def test_nfl_moneyline_requires_manual_review_even_with_clock_and_lead() -> None:
    game = LiveGameState(
        league="NFL",
        home_name="Kansas City Chiefs",
        away_name="Denver Broncos",
        home_score=27,
        away_score=17,
        period="Q4",
        status=LiveGameStatus.LIVE,
        seconds_remaining=90,
    )
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=SportsMarketSide.HOME,
        token_id="home",
        line=None,
        best_ask=Decimal("0.92"),
        buyable_liquidity_usdc=Decimal("10"),
    )

    result = evaluate_tail_opportunity(game, market, policy=SportsTailPolicy(min_moneyline_lead=6))

    assert result.accepted is True
    assert result.action == TailAction.MANUAL_CONFIRM
    assert result.reason == "nfl_requires_manual_review"


def test_tennis_totals_over_locked_can_auto_execute_from_live_games_state() -> None:
    game = LiveGameState(
        league="WTA 125K Huzhou, China Women Singles",
        home_name="Rada Zolotareva",
        away_name="Despina Papamichail",
        home_score=1,
        away_score=1,
        period="S3",
        status=LiveGameStatus.LIVE,
        tennis_state=TennisGameState(
            home_sets_won=1,
            away_sets_won=1,
            current_set=3,
            home_current_set_games=1,
            away_current_set_games=1,
            home_total_games=10,
            away_total_games=12,
        ),
    )
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.TOTALS,
        side=SportsMarketSide.OVER,
        token_id="over",
        line=Decimal("21.5"),
        best_ask=Decimal("0.96"),
        buyable_liquidity_usdc=Decimal("10"),
    )

    result = evaluate_tail_opportunity(game, market, policy=SportsTailPolicy())

    assert result.accepted is True
    assert result.action == TailAction.AUTO_EXECUTE
    assert result.reason == "tennis_totals_over_locked"


def test_tennis_set_totals_use_set_count_not_total_games() -> None:
    game = LiveGameState(
        league="WTA 125K Huzhou, China Women Singles",
        home_name="Hanyu Guo",
        away_name="Dalila Jakupovic",
        home_score=1,
        away_score=0,
        period="S2",
        status=LiveGameStatus.LIVE,
        tennis_state=TennisGameState(
            home_sets_won=1,
            away_sets_won=0,
            current_set=2,
            home_current_set_games=1,
            away_current_set_games=2,
            home_total_games=7,
            away_total_games=5,
        ),
    )
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.TOTALS,
        side=SportsMarketSide.OVER,
        token_id="over",
        line=Decimal("2.5"),
        best_ask=Decimal("0.96"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug="wta-guo-jakupov-2026-04-27-set-totals-2pt5",
    )

    result = evaluate_tail_opportunity(game, market, policy=SportsTailPolicy())

    assert result.accepted is False
    assert result.reason == "tennis_not_late_enough"


def test_tennis_set_totals_over_locked_when_deciding_set_started() -> None:
    game = LiveGameState(
        league="WTA 125K Huzhou, China Women Singles",
        home_name="Anastasia Zolotareva",
        away_name="Chengyiyi Yuan",
        home_score=1,
        away_score=1,
        period="S3",
        status=LiveGameStatus.LIVE,
        tennis_state=TennisGameState(
            home_sets_won=1,
            away_sets_won=1,
            current_set=3,
            home_current_set_games=2,
            away_current_set_games=3,
            home_total_games=11,
            away_total_games=10,
        ),
    )
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.TOTALS,
        side=SportsMarketSide.OVER,
        token_id="over",
        line=Decimal("2.5"),
        best_ask=Decimal("0.96"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug="wta-zolota-yua-2026-04-27-set-totals-2pt5",
    )

    result = evaluate_tail_opportunity(game, market, policy=SportsTailPolicy())

    assert result.accepted is True
    assert result.reason == "tennis_set_totals_over_locked"


def test_tennis_first_set_winner_is_not_treated_as_match_moneyline() -> None:
    game = LiveGameState(
        league="WTA 125K Huzhou, China Women Singles",
        home_name="Hanyu Guo",
        away_name="Dalila Jakupovic",
        home_score=1,
        away_score=0,
        period="S2",
        status=LiveGameStatus.LIVE,
        tennis_state=TennisGameState(
            home_sets_won=1,
            away_sets_won=0,
            current_set=2,
            home_current_set_games=5,
            away_current_set_games=2,
            home_total_games=11,
            away_total_games=6,
        ),
    )
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=SportsMarketSide.HOME,
        token_id="home",
        line=None,
        best_ask=Decimal("0.10"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug="wta-guo-jakupov-2026-04-27-first-set-winner-Guo-vs-Jakupovic",
    )

    result = evaluate_tail_opportunity(game, market, policy=SportsTailPolicy())

    assert result.accepted is False
    assert result.reason == "tennis_set_winner_not_supported"


def test_tennis_first_set_winner_locked_from_per_set_score() -> None:
    game = LiveGameState(
        league="WTA 125K Huzhou, China Women Singles",
        home_name="Hanyu Guo",
        away_name="Dalila Jakupovic",
        home_score=1,
        away_score=0,
        period="S2",
        status=LiveGameStatus.LIVE,
        tennis_state=TennisGameState(
            home_sets_won=1,
            away_sets_won=0,
            current_set=2,
            home_current_set_games=5,
            away_current_set_games=2,
            home_total_games=11,
            away_total_games=6,
            set_scores=((6, 4), (5, 2)),
        ),
    )
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=SportsMarketSide.HOME,
        token_id="home",
        line=None,
        best_ask=Decimal("0.10"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug="wta-guo-jakupov-2026-04-27-first-set-winner-Guo-vs-Jakupovic",
    )

    result = evaluate_tail_opportunity(game, market, policy=SportsTailPolicy())

    assert result.accepted is True
    assert result.action == TailAction.AUTO_EXECUTE
    assert result.reason == "tennis_set_winner_locked"


def test_tennis_state_uses_wider_freshness_window_than_clock_sports() -> None:
    observed_at = datetime(2026, 4, 28, 7, 0, tzinfo=timezone.utc)
    game = LiveGameState(
        league="WTA 125K Huzhou, China Women Singles",
        home_name="Anastasia Zolotareva",
        away_name="Chengyiyi Yuan",
        home_score=1,
        away_score=1,
        period="S3",
        status=LiveGameStatus.LIVE,
        observed_at=observed_at,
        tennis_state=TennisGameState(
            home_sets_won=1,
            away_sets_won=1,
            current_set=3,
            home_current_set_games=2,
            away_current_set_games=3,
            home_total_games=11,
            away_total_games=10,
        ),
    )
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.TOTALS,
        side=SportsMarketSide.OVER,
        token_id="over",
        line=Decimal("2.5"),
        best_ask=Decimal("0.96"),
        buyable_liquidity_usdc=Decimal("10"),
        market_slug="wta-zolota-yua-2026-04-27-set-totals-2pt5",
    )

    result = evaluate_tail_opportunity(
        game,
        market,
        policy=SportsTailPolicy(max_game_state_age_seconds=10, tennis_max_game_state_age_seconds=35),
        now=datetime(2026, 4, 28, 7, 0, 25, tzinfo=timezone.utc),
    )

    assert result.accepted is True
    assert result.reason == "tennis_set_totals_over_locked"


def test_tennis_moneyline_requires_near_locked_current_set() -> None:
    game = LiveGameState(
        league="ATP Challenger",
        home_name="Player A",
        away_name="Player B",
        home_score=1,
        away_score=0,
        period="S2",
        status=LiveGameStatus.LIVE,
        tennis_state=TennisGameState(
            home_sets_won=1,
            away_sets_won=0,
            current_set=2,
            home_current_set_games=5,
            away_current_set_games=2,
            home_total_games=11,
            away_total_games=6,
        ),
    )
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=SportsMarketSide.HOME,
        token_id="home",
        line=None,
        best_ask=Decimal("0.94"),
        buyable_liquidity_usdc=Decimal("10"),
    )

    result = evaluate_tail_opportunity(game, market, policy=SportsTailPolicy())

    assert result.accepted is True
    assert result.reason == "tennis_moneyline_near_locked"


def test_default_discovery_scope_matches_live_source_coverage() -> None:
    config = CurrentStrategyConfig()
    settings = Settings(_env_file=None)

    assert config.discovery_title_searches == ("nba", "nhl", "nfl", "mlb", "tennis", "atp", "wta")
    assert "sports" not in config.sports_category_tokens
    assert {"nba", "nhl", "nfl", "mlb", "basketball", "hockey", "football", "baseball"} <= set(
        config.sports_category_tokens
    )
    assert "soccer" in config.sports_category_tokens
    assert {"table tennis", "table-tennis", "wtt"} <= set(config.sports_category_tokens)
    assert {"tennis", "atp", "wta"} <= set(config.sports_category_tokens)
    assert settings.sports_live_state_sofascore_lookahead_days >= 3
