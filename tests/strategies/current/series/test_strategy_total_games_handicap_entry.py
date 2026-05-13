"""strategy.decide_entry 在 TOTAL_GAMES / GAME_HANDICAP 子类型上的端到端契约。

覆盖：
- 缺数据 → SKIP + 可审计 reason
- AUTO_EXECUTE + budget 解锁 + 完整 metadata → BUY 决策
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.extension_api import MarketTokenView
from polymarket_trader.extension_api.context import ExtensionContext
from polymarket_trader.extension_api.decisions import ExtensionAction
from strategies.current.config import CurrentStrategyConfig
from strategies.current.identity import STRATEGY_ID
from strategies.current.strategy import CurrentStrategy
from strategies.current.tail.types import ExecutionPermission


_NOW = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)


def _orderbook(*, best_ask: Decimal, ask_size: Decimal = Decimal("100")) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id="tok",
        best_bid=Decimal("0.30"),
        best_ask=best_ask,
        bids=(PriceLevel(price=Decimal("0.30"), size=Decimal("100")),),
        asks=(PriceLevel(price=best_ask, size=ask_size),),
        received_at=_NOW,
        tick_size=Decimal("0.01"),
    )


def _series_state_meta(*, wins_a: int = 0, wins_b: int = 0) -> dict:
    return {
        "team_a": "Boston Celtics",
        "team_b": "New York Knicks",
        "wins_a": wins_a,
        "wins_b": wins_b,
        "best_of": 7,
        "observed_at": _NOW.isoformat(),
        "next_game_at": None,
    }


def _game_odds_meta(p_a: str = "0.5") -> dict:
    return {
        "team_a": "Boston Celtics",
        "team_b": "New York Knicks",
        "p_a": p_a,
        "observed_at": _NOW.isoformat(),
        "source": "theoddsapi",
    }


def _spreads_meta(*, spread_line: str = "-3.5", p_a_covers: str = "0.55") -> dict:
    return {
        "team_a": "Boston Celtics",
        "team_b": "New York Knicks",
        "spread_line": spread_line,
        "p_a_covers": p_a_covers,
        "observed_at": _NOW.isoformat(),
        "source": "theoddsapi",
    }


def _context(*, market: Market, metadata: dict, token_views: tuple[MarketTokenView, ...]) -> ExtensionContext:
    return ExtensionContext(
        trace_id="trace-series-multi",
        strategy_id=STRATEGY_ID,
        market=market,
        metadata=metadata,
        now=_NOW,
        market_token_views=token_views,
    )


def _total_games_market() -> Market:
    return Market(
        condition_id="cond-totalgames",
        market_slug="celtics-knicks-total-games-5pt5",
        market_question="Celtics vs Knicks Total Games O/U 5.5",
        event_title="NBA Playoffs Series Total Games",
        event_slug="celtics-knicks-2026-playoffs",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="tok-over", outcome="Over 5.5"),
            MarketOutcome(token_id="tok-under", outcome="Under 5.5"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _handicap_market() -> Market:
    return Market(
        condition_id="cond-handicap",
        market_slug="celtics-knicks-series-handicap",
        market_question="Celtics series handicap -1.5",
        event_title="NBA Playoffs Series Handicap",
        event_slug="celtics-knicks-2026-playoffs",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="tok-celtics", outcome="Celtics -1.5"),
            MarketOutcome(token_id="tok-knicks", outcome="Knicks +1.5"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def test_total_games_buy_when_unlocked() -> None:
    config = replace(
        CurrentStrategyConfig(),
        tail_series_total_games_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_series_total_games_budget_usdc=Decimal("100"),
        tail_series_total_games_max_per_market_usdc=Decimal("25"),
        tail_series_total_games_min_edge_bps=300,
        tail_series_total_games_max_entry_price=Decimal("0.99"),
        tail_series_total_games_min_orderbook_depth_usdc=Decimal("20"),
    )
    strategy = CurrentStrategy(config=config)
    market = _total_games_market()
    metadata = {
        "series_state": _series_state_meta(),
        "game_odds": _game_odds_meta(p_a="0.5"),
    }
    token_views = (
        MarketTokenView(token_id="tok-over", outcome="Over 5.5", orderbook=_orderbook(best_ask=Decimal("0.35"))),
        MarketTokenView(token_id="tok-under", outcome="Under 5.5", orderbook=_orderbook(best_ask=Decimal("0.35"))),
    )
    decision = strategy.decide_entry(_context(market=market, metadata=metadata, token_views=token_views))
    assert decision.action == ExtensionAction.BUY
    assert decision.metadata.get("series_sub_type") == "total_games"
    assert decision.price is not None
    assert decision.amount_usdc is not None
    assert decision.amount_usdc > Decimal("0")


def test_handicap_series_scope_buy_when_unlocked() -> None:
    config = replace(
        CurrentStrategyConfig(),
        tail_series_handicap_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_series_handicap_budget_usdc=Decimal("100"),
        tail_series_handicap_max_per_market_usdc=Decimal("25"),
        tail_series_handicap_min_edge_bps=300,
        tail_series_handicap_max_entry_price=Decimal("0.99"),
        tail_series_handicap_min_orderbook_depth_usdc=Decimal("20"),
    )
    strategy = CurrentStrategy(config=config)
    market = _handicap_market()
    metadata = {
        "series_state": _series_state_meta(),
        "game_odds": _game_odds_meta(p_a="0.6"),
    }
    # 0-0 best-of-7, p_a=0.6, Celtics -1.5（需多赢 ≥2 场）→ fair 显著
    token_views = (
        MarketTokenView(token_id="tok-celtics", outcome="Celtics -1.5", orderbook=_orderbook(best_ask=Decimal("0.30"))),
        MarketTokenView(token_id="tok-knicks", outcome="Knicks +1.5", orderbook=_orderbook(best_ask=Decimal("0.30"))),
    )
    decision = strategy.decide_entry(_context(market=market, metadata=metadata, token_views=token_views))
    assert decision.action == ExtensionAction.BUY
    assert decision.metadata.get("series_sub_type") == "game_handicap"


def test_handicap_single_game_skip_when_spreads_missing() -> None:
    config = replace(
        CurrentStrategyConfig(),
        tail_series_handicap_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_series_handicap_budget_usdc=Decimal("100"),
    )
    strategy = CurrentStrategy(config=config)
    market = Market(
        condition_id="cond-handicap-game5",
        market_slug="celtics-knicks-game-5-handicap",
        market_question="Celtics -3.5 in Game 5",
        event_title="NBA Playoffs Game 5 Handicap",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="tok-celtics", outcome="Celtics -3.5"),
            MarketOutcome(token_id="tok-knicks", outcome="Knicks +3.5"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    metadata = {"series_state": _series_state_meta()}  # 缺 game_spreads
    token_views = (
        MarketTokenView(token_id="tok-celtics", outcome="Celtics -3.5", orderbook=_orderbook(best_ask=Decimal("0.30"))),
        MarketTokenView(token_id="tok-knicks", outcome="Knicks +3.5", orderbook=_orderbook(best_ask=Decimal("0.30"))),
    )
    decision = strategy.decide_entry(_context(market=market, metadata=metadata, token_views=token_views))
    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("series_sub_type") == "game_handicap"
    assert decision.metadata.get("series_reject_reason") == "missing_game_spreads"


def test_handicap_single_game_buy_when_spreads_aligned() -> None:
    config = replace(
        CurrentStrategyConfig(),
        tail_series_handicap_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_series_handicap_budget_usdc=Decimal("100"),
        tail_series_handicap_max_per_market_usdc=Decimal("25"),
        tail_series_handicap_min_edge_bps=300,
        tail_series_handicap_max_entry_price=Decimal("0.99"),
        tail_series_handicap_min_orderbook_depth_usdc=Decimal("20"),
    )
    strategy = CurrentStrategy(config=config)
    market = Market(
        condition_id="cond-handicap-game5",
        market_slug="celtics-knicks-game-5-handicap",
        market_question="Celtics -3.5 in Game 5",
        event_title="NBA Playoffs Game 5 Handicap",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="tok-celtics", outcome="Celtics -3.5"),
            MarketOutcome(token_id="tok-knicks", outcome="Knicks +3.5"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    metadata = {
        "series_state": _series_state_meta(),
        "game_spreads": _spreads_meta(spread_line="-3.5", p_a_covers="0.55"),
    }
    token_views = (
        MarketTokenView(token_id="tok-celtics", outcome="Celtics -3.5", orderbook=_orderbook(best_ask=Decimal("0.30"))),
        MarketTokenView(token_id="tok-knicks", outcome="Knicks +3.5", orderbook=_orderbook(best_ask=Decimal("0.30"))),
    )
    decision = strategy.decide_entry(_context(market=market, metadata=metadata, token_views=token_views))
    assert decision.action == ExtensionAction.BUY
    assert decision.metadata.get("series_sub_type") == "game_handicap"
    series_meta = decision.metadata.get("series_metadata")
    assert series_meta is not None
    assert series_meta.get("handicap_scope") == "single_game"
