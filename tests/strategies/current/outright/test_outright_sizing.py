from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.extension_api.context import ExtensionContext
from strategies.current.config import CurrentStrategyConfig
from strategies.current.identity import STRATEGY_ID
from strategies.current.strategy import CurrentStrategy


def _outright_market() -> Market:
    return Market(
        condition_id="nba-champion-2026",
        market_slug="will-celtics-win-2026-nba-championship",
        market_question="Will Celtics win 2026 NBA championship?",
        event_title="2026 NBA Champion",
        event_slug="2026-nba-championship-winner",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="yes", outcome="Yes"),
            MarketOutcome(token_id="no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _ctx(market: Market) -> ExtensionContext:
    return ExtensionContext(
        trace_id="t",
        strategy_id=STRATEGY_ID,
        market=market,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )


def test_size_entry_returns_zero_for_outright_with_default_budget() -> None:
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    sizing = strategy.size_entry(_ctx(_outright_market()))
    assert sizing.reason == "outright_budget_zero"
    assert sizing.allocation_plan.total_budget_usdc == Decimal("0")


def test_size_entry_caps_outright_at_per_market_limit() -> None:
    config = replace(
        CurrentStrategyConfig(),
        tail_outright_budget_usdc=Decimal("100"),
        tail_outright_max_per_market_usdc=Decimal("25"),
    )
    strategy = CurrentStrategy(config=config)
    sizing = strategy.size_entry(_ctx(_outright_market()))
    assert sizing.reason == "outright_sizing"
    assert sizing.allocation_plan.total_budget_usdc == Decimal("100")
    # per-market 上限通过 metadata 透出，供 decide_entry 单笔限额参考。
    assert sizing.metadata.get("outright_per_market_usdc") == "25"
