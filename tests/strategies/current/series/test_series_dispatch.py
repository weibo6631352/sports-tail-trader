"""``CurrentStrategy.decide_entry`` 对 series family 的分派契约。

所有子类型都进 ``series.evaluator``；缺数据时返回 SKIP 决策携带可审计 reject_reason
（如 missing_series_state / series_outcome_not_parsed），accepted 路径产 BUY。
不得构造旁路 / 不得绕过风控。
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


def test_series_winner_dispatch_skips_when_state_missing() -> None:
    # Worktree 3：WINNER classifier 命中后，缺 SeriesState（worker 未写入 metadata）
    # 走 MISSING_SERIES_STATE 可审计拒绝；不构造 BUY、不旁路风控。
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    decision = strategy.decide_entry(_context(_series_winner_market()))

    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("market_family") == "series"
    assert decision.metadata.get("series_sub_type") == "winner"
    assert decision.metadata.get("series_reject_reason") == "missing_series_state"
    assert decision.metadata.get("series_accepted") is False


def test_series_total_games_dispatch_skips_when_state_missing() -> None:
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    decision = strategy.decide_entry(_context(_series_total_games_market()))

    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("market_family") == "series"
    assert decision.metadata.get("series_sub_type") == "total_games"
    # 缺 series_state → missing_series_state。
    assert decision.metadata.get("series_reject_reason") == "missing_series_state"


def test_series_game_handicap_dispatch_skips_when_state_missing() -> None:
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    decision = strategy.decide_entry(_context(_series_game_handicap_market()))

    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("market_family") == "series"
    assert decision.metadata.get("series_sub_type") == "game_handicap"
    assert decision.metadata.get("series_reject_reason") == "missing_series_state"


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
