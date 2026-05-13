"""strategy.decide_entry 在 series WINNER 路径上的端到端契约。

覆盖：
- 缺数据 → SKIP + 可审计 reason
- AUTO_EXECUTE + budget 解锁 + 完整 metadata → BUY 决策（route 通过 framework
  RiskManager 才是最终下单门禁，本测试只验 strategy 产出的 ExtensionDecision）
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


def _market() -> Market:
    return Market(
        condition_id="cond-series",
        market_slug="celtics-vs-knicks-series-winner",
        market_question="Will Celtics win the series?",
        event_title="NBA Playoffs Series Winner",
        event_slug="celtics-vs-knicks-2026-playoffs",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="tok-yes", outcome="Yes"),
            MarketOutcome(token_id="tok-no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _series_state_meta(*, wins_a: int = 2, wins_b: int = 1) -> dict:
    return {
        "team_a": "Boston Celtics",
        "team_b": "New York Knicks",
        "wins_a": wins_a,
        "wins_b": wins_b,
        "best_of": 7,
        "observed_at": _NOW.isoformat(),
        "next_game_at": None,
    }


def _game_odds_meta(p_a: str = "0.6") -> dict:
    return {
        "team_a": "Boston Celtics",
        "team_b": "New York Knicks",
        "p_a": p_a,
        "observed_at": _NOW.isoformat(),
        "source": "theoddsapi",
    }


def _orderbook(*, best_ask: Decimal, ask_size: Decimal = Decimal("100"), bid: Decimal = Decimal("0.30")) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id="tok",
        best_bid=bid,
        best_ask=best_ask,
        bids=(PriceLevel(price=bid, size=Decimal("100")),),
        asks=(PriceLevel(price=best_ask, size=ask_size),),
        received_at=_NOW,
        tick_size=Decimal("0.01"),
    )


def _context(*, market: Market, metadata: dict, token_views: tuple[MarketTokenView, ...]) -> ExtensionContext:
    return ExtensionContext(
        trace_id="trace-series",
        strategy_id=STRATEGY_ID,
        market=market,
        metadata=metadata,
        now=_NOW,
        market_token_views=token_views,
    )


def test_series_winner_skips_with_missing_series_state() -> None:
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    market = _market()
    token_views = (
        MarketTokenView(token_id="tok-yes", outcome="Yes", orderbook=_orderbook(best_ask=Decimal("0.50"))),
        MarketTokenView(token_id="tok-no", outcome="No", orderbook=_orderbook(best_ask=Decimal("0.50"))),
    )
    decision = strategy.decide_entry(
        _context(market=market, metadata={}, token_views=token_views)
    )
    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("series_reject_reason") == "missing_series_state"


def test_series_winner_skips_with_missing_game_odds() -> None:
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    market = _market()
    metadata = {"series_state": _series_state_meta()}
    token_views = (
        MarketTokenView(token_id="tok-yes", outcome="Yes", orderbook=_orderbook(best_ask=Decimal("0.50"))),
        MarketTokenView(token_id="tok-no", outcome="No", orderbook=_orderbook(best_ask=Decimal("0.50"))),
    )
    decision = strategy.decide_entry(
        _context(market=market, metadata=metadata, token_views=token_views)
    )
    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("series_reject_reason") == "missing_series_odds"


def test_series_winner_record_only_when_permission_locked() -> None:
    """Record-only：accepted 但 permission!=AUTO_EXECUTE → SKIP 带审计 metadata。"""

    config = replace(
        CurrentStrategyConfig(),
        tail_series_winner_execution_permission=ExecutionPermission.RECORD_ONLY,
        tail_series_winner_budget_usdc=Decimal("0"),
    )
    strategy = CurrentStrategy(config=config)
    market = _market()
    metadata = {
        "series_state": _series_state_meta(),
        "game_odds": _game_odds_meta(),
    }
    token_views = (
        MarketTokenView(token_id="tok-yes", outcome="Yes", orderbook=_orderbook(best_ask=Decimal("0.50"))),
        MarketTokenView(token_id="tok-no", outcome="No", orderbook=_orderbook(best_ask=Decimal("0.50"))),
    )
    decision = strategy.decide_entry(
        _context(market=market, metadata=metadata, token_views=token_views)
    )
    # 默认 RECORD_ONLY + budget=0 → 不构造 BUY
    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("market_family") == "series"
    assert decision.metadata.get("series_accepted") is True
    assert decision.metadata.get("execution_permission") == "record_only"
    assert decision.metadata.get("budget_unlocked") is False


def test_series_winner_constructs_buy_when_fully_unlocked() -> None:
    config = replace(
        CurrentStrategyConfig(),
        tail_series_winner_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_series_winner_budget_usdc=Decimal("100"),
        tail_series_winner_max_per_market_usdc=Decimal("25"),
        tail_series_winner_min_edge_bps=500,  # 让 cap 较宽，best_ask=0.50 能通过
        tail_series_winner_max_entry_price=Decimal("0.99"),
        tail_series_winner_min_orderbook_depth_usdc=Decimal("20"),
    )
    strategy = CurrentStrategy(config=config)
    market = _market()
    metadata = {
        "series_state": _series_state_meta(),
        "game_odds": _game_odds_meta(),
    }
    token_views = (
        MarketTokenView(token_id="tok-yes", outcome="Yes", orderbook=_orderbook(best_ask=Decimal("0.50"))),
        MarketTokenView(token_id="tok-no", outcome="No", orderbook=_orderbook(best_ask=Decimal("0.50"))),
    )
    decision = strategy.decide_entry(
        _context(market=market, metadata=metadata, token_views=token_views)
    )
    assert decision.action == ExtensionAction.BUY
    assert decision.token_id == "tok-yes"  # Yes 对 team_a，2-1 lead 时 fair ≈ 0.82 > 0.50
    assert decision.price is not None
    assert decision.amount_usdc is not None
    assert decision.amount_usdc > Decimal("0")
    assert decision.metadata.get("market_family") == "series"
    series_meta = decision.metadata.get("series_metadata")
    assert series_meta is not None
    assert "fair_value" in series_meta
    assert "entry_price_cap" in series_meta
