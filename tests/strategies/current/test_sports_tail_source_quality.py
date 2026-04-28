from __future__ import annotations

from decimal import Decimal

from strategies.current.sports_tail import (
    BaseballGameState,
    LiveGameState,
    LiveGameStatus,
    TailAction,
    SportsMarketSide,
    SportsMarketSnapshot,
    SportsMarketType,
    SportsTailPolicy,
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


def test_default_discovery_scope_matches_live_source_coverage() -> None:
    config = CurrentStrategyConfig()

    assert config.discovery_title_searches == ("sports", "nba", "nhl", "nfl", "mlb")
    assert "sports" not in config.sports_category_tokens
    assert "soccer" not in config.sports_category_tokens
    assert "tennis" not in config.sports_category_tokens
    assert {"nba", "nhl", "nfl", "mlb", "basketball", "hockey", "football", "baseball"} <= set(
        config.sports_category_tokens
    )
