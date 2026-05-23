"""sports_framework 公共 API 烟测：包独立可用且无对策略包反向依赖。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.sports_live import BaseballGameState
from strategies.sports_framework import (
    LiveGameState,
    LiveGameStatus,
    SportsMarketFamily,
    SportsMarketScopeType,
    SportsMarketSide,
    SportsMarketSnapshot,
    SportsMarketType,
    TennisGameState,
    is_mlb_game,
    is_nfl_game,
    is_tennis_game,
    is_tennis_scope_candidate,
    is_unsupported_period_total,
    live_game_state_from_metadata,
    market_scope,
    normalized_market_slug,
    totals_market_scope,
)


def _market(slug: str, market_type: SportsMarketType = SportsMarketType.TOTALS) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=market_type,
        side=SportsMarketSide.OVER,
        token_id="t1",
        line=Decimal("8.5"),
        best_ask=Decimal("0.5"),
        buyable_liquidity_usdc=Decimal("100"),
        market_family=SportsMarketFamily.SINGLE_GAME,
        market_slug=slug,
    )


def _game(league: str, *, tennis_state: TennisGameState | None = None) -> LiveGameState:
    return LiveGameState(
        league=league,
        home_name="A",
        away_name="B",
        home_score=1,
        away_score=2,
        period="bot 9th",
        status=LiveGameStatus.LIVE,
        tennis_state=tennis_state,
    )


def test_league_recognition() -> None:
    assert is_mlb_game(_game("MLB"))
    assert is_mlb_game(_game("KBO"))
    assert is_nfl_game(_game("NFL"))
    assert is_tennis_game(_game("ATP"))
    assert is_tennis_game(_game("Some League", tennis_state=TennisGameState()))
    assert not is_mlb_game(_game("NBA"))
    assert not is_tennis_game(_game("MLB"))


def test_normalized_market_slug() -> None:
    market = _market("First-Set_Total")
    assert normalized_market_slug(market) == "first set total"


def test_totals_market_scope_recognizes_tennis_set_games() -> None:
    market = _market("First Set Total")
    scope = totals_market_scope(market)
    assert scope.scope_type == SportsMarketScopeType.TENNIS_SET_GAMES
    assert scope.scope_number == 1


def test_totals_market_scope_unsupported_period() -> None:
    market = _market("First Quarter Total")
    scope = totals_market_scope(market)
    assert scope.scope_type == SportsMarketScopeType.UNSUPPORTED_PERIOD


def test_market_scope_falls_back_to_full_game_for_moneyline() -> None:
    market = _market("game winner", market_type=SportsMarketType.MONEYLINE)
    scope = market_scope(market)
    assert scope.scope_type == SportsMarketScopeType.FULL_GAME


def test_is_tennis_scope_candidate_uses_slug() -> None:
    assert is_tennis_scope_candidate(_market("ATP Madrid Final"))
    assert not is_tennis_scope_candidate(_market("MLB Game"))


def test_is_unsupported_period_total_text() -> None:
    assert is_unsupported_period_total("first 5 innings total")
    assert not is_unsupported_period_total("total runs")


def test_live_game_state_from_metadata_baseball() -> None:
    metadata = {
        "live_game": {
            "league": "MLB",
            "home_name": "Yankees",
            "away_name": "Red Sox",
            "home_score": 3,
            "away_score": 2,
            "period": "bot 9th",
            "status": "live",
            "seconds_remaining": 60,
            "observed_at": datetime(2026, 5, 11, tzinfo=timezone.utc).isoformat(),
            "baseball_state": {
                "current_inning": 9,
                "inning_half": "bottom",
                "outs": 2,
                "occupied_bases": [1, 3],
            },
        }
    }
    game = live_game_state_from_metadata(metadata)
    assert game is not None
    assert game.status == LiveGameStatus.LIVE
    assert game.home_score == 3
    assert isinstance(game.baseball_state, BaseballGameState)
    assert game.baseball_state.current_inning == 9
    assert game.baseball_state.occupied_bases == (1, 3)


def test_live_game_state_from_metadata_top_level() -> None:
    metadata = {
        "league": "ATP",
        "home_name": "Alcaraz",
        "away_name": "Sinner",
        "home_score": 1,
        "away_score": 0,
        "period": "set 2",
        "status": "live",
        "tennis_state": {
            "home_sets_won": 1,
            "away_sets_won": 0,
            "current_set": 2,
            "home_total_games": 7,
            "away_total_games": 5,
            "set_scores": [{"home": 6, "away": 4}],
        },
    }
    game = live_game_state_from_metadata(metadata)
    assert game is not None
    assert game.tennis_state is not None
    assert game.tennis_state.home_sets_won == 1
    assert game.tennis_state.set_scores == ((6, 4),)


def test_live_game_state_returns_none_on_missing_scores() -> None:
    assert live_game_state_from_metadata({}) is None


def test_live_game_state_live_feed_lag_seconds_from_server_clock() -> None:
    """server_clock_at 经 metadata → LiveGameState → lag_seconds 算 stale 程度。"""
    server_clock = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)
    metadata = {
        "live_game": {
            "league": "MLB",
            "home_name": "A",
            "away_name": "B",
            "home_score": 0,
            "away_score": 0,
            "status": "live",
            "server_clock_at": server_clock.isoformat(),
        }
    }
    game = live_game_state_from_metadata(metadata)
    assert game is not None
    assert game.server_clock_at == server_clock
    # now = server_clock + 7s
    now = datetime(2026, 5, 23, 12, 0, 7, tzinfo=timezone.utc)
    assert game.live_feed_lag_seconds(now=now) == 7.0


def test_live_game_state_lag_returns_none_when_no_server_clock() -> None:
    """无 server_clock_at（旧数据 / 非 Goalserve 源）→ lag = None，决策侧应不降级。"""
    metadata = {
        "live_game": {
            "league": "MLB", "home_name": "A", "away_name": "B",
            "home_score": 0, "away_score": 0, "status": "live",
        }
    }
    game = live_game_state_from_metadata(metadata)
    assert game is not None
    assert game.server_clock_at is None
    assert game.live_feed_lag_seconds() is None


def test_sports_framework_does_not_import_strategy_modules() -> None:
    """sports_framework 必须单向依赖 domain，绝不反向依赖任何具体策略包。"""

    import strategies.sports_framework as pkg
    import strategies.sports_framework.leagues as leagues
    import strategies.sports_framework.parsing as parsing
    import strategies.sports_framework.slug as slug
    import strategies.sports_framework.types as types

    for module in (pkg, leagues, parsing, slug, types):
        for name in dir(module):
            attr = getattr(module, name)
            module_name = getattr(attr, "__module__", "")
            assert not module_name.startswith("strategies.current"), (
                f"sports_framework 不允许引用具体策略包: {module.__name__} -> {module_name}.{name}"
            )
