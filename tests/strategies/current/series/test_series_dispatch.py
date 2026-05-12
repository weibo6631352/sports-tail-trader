"""``CurrentStrategy.decide_entry`` 对 series family 的分派契约。

worktree 2 阶段：所有子类型都应进 ``series.evaluator`` 并返回 SKIP 决策携带
``*_MODEL_PENDING`` reject_reason；不得构造 BUY、不得旁路风控、不得回落 tail 路径。
"""

from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.extension_api.context import ExtensionContext
from polymarket_trader.extension_api.decisions import ExtensionAction
from strategies.current.config import CurrentStrategyConfig
from strategies.current.identity import STRATEGY_ID
from strategies.current.strategy import CurrentStrategy


def _context(market: Market) -> ExtensionContext:
    return ExtensionContext(
        trace_id="trace-series-dispatch",
        strategy_id=STRATEGY_ID,
        market=market,
        metadata={},
        now=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )


def _series_winner_market() -> Market:
    return Market(
        condition_id="series-winner-cond",
        market_slug="nba-playoffs-who-will-win-series",
        market_question="NBA Playoffs: Who Will Win Series? - Knicks vs. Hawks",
        event_title="NBA Playoffs Series Winner",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="series-knicks", outcome="Knicks"),
            MarketOutcome(token_id="series-hawks", outcome="Hawks"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _series_total_games_market() -> Market:
    return Market(
        condition_id="series-totals-cond",
        market_slug="nhl-playoffs-total-games-ou-5pt5",
        market_question="NHL Playoffs: Ducks vs. Oilers Total Games O/U 5.5",
        event_title="NHL Playoffs Total Games",
        category="Sports",
        tags=("NHL",),
        outcomes=(
            MarketOutcome(token_id="games-over", outcome="Over 5.5"),
            MarketOutcome(token_id="games-under", outcome="Under 5.5"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _series_game_handicap_market() -> Market:
    return Market(
        condition_id="series-handicap-cond",
        market_slug="nba-game-5-handicap",
        market_question="NBA Game 5 handicap: Lakers -3.5 (series spread)",
        event_title="NBA Game 5 Handicap",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="lakers-minus", outcome="Lakers -3.5"),
            MarketOutcome(token_id="opponent-plus", outcome="Opponent +3.5"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def test_series_winner_dispatch_returns_skip_with_winner_pending() -> None:
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    decision = strategy.decide_entry(_context(_series_winner_market()))

    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("market_family") == "series"
    assert decision.metadata.get("series_sub_type") == "winner"
    assert decision.metadata.get("series_reject_reason") == "winner_model_pending"
    assert decision.metadata.get("series_accepted") is False


def test_series_total_games_dispatch_returns_skip_with_total_games_pending() -> None:
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    decision = strategy.decide_entry(_context(_series_total_games_market()))

    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("market_family") == "series"
    assert decision.metadata.get("series_sub_type") == "total_games"
    assert decision.metadata.get("series_reject_reason") == "total_games_model_pending"


def test_series_game_handicap_dispatch_returns_skip_with_handicap_pending() -> None:
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    decision = strategy.decide_entry(_context(_series_game_handicap_market()))

    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("market_family") == "series"
    assert decision.metadata.get("series_sub_type") == "game_handicap"
    assert decision.metadata.get("series_reject_reason") == "handicap_model_pending"


def test_series_dispatch_does_not_construct_buy() -> None:
    # 双保险：哪种子类型都不能产生 BUY 或 SELL 决策，避免绕过 risk/executor。
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    for market_fixture in (
        _series_winner_market(),
        _series_total_games_market(),
        _series_game_handicap_market(),
    ):
        decision = strategy.decide_entry(_context(market_fixture))
        assert decision.action == ExtensionAction.SKIP
        assert decision.price is None
        assert decision.amount_usdc is None
