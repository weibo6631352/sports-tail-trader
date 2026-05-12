from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.extension_api.context import ExtensionContext
from polymarket_trader.extension_api.decisions import MarketTokenView
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


_NOW = datetime(2026, 5, 11, tzinfo=timezone.utc)


def _orderbook(token_id: str, ask_price: str) -> OrderbookSnapshot:
    from polymarket_trader.domain.orderbook import PriceLevel

    price = Decimal(ask_price)
    return OrderbookSnapshot(
        token_id=token_id,
        best_bid=price - Decimal("0.01"),
        best_ask=price,
        received_at=_NOW,
        asks=(PriceLevel(price=price, size=Decimal("500")),),
        bids=(),
    )


def _ctx(
    market: Market,
    *,
    with_kelly: bool = False,
    metadata: dict | None = None,
) -> ExtensionContext:
    token_views: tuple[MarketTokenView, ...] = ()
    kelly_fraction = None
    kelly_max_position_fraction = None
    kelly_min_stake_usdc = None
    if with_kelly:
        token_views = (
            MarketTokenView(token_id="yes", outcome="Yes", orderbook=_orderbook("yes", "0.20")),
            MarketTokenView(token_id="no", outcome="No", orderbook=_orderbook("no", "0.85")),
        )
        kelly_fraction = Decimal("0.25")
        kelly_max_position_fraction = Decimal("0.10")
        kelly_min_stake_usdc = Decimal("1")
    return ExtensionContext(
        trace_id="t",
        strategy_id=STRATEGY_ID,
        market=market,
        now=_NOW,
        market_token_views=token_views,
        kelly_fraction=kelly_fraction,
        kelly_max_position_fraction=kelly_max_position_fraction,
        kelly_min_stake_usdc=kelly_min_stake_usdc,
        bankroll_usdc=Decimal("1000"),
        metadata=metadata,
    )


def _season_odds_meta(probs: dict[str, str]) -> dict:
    """构造 season_odds_from_metadata() 可以解析的 metadata 结构。"""
    return {
        "season_odds_snapshot": {
            "market_key": "nba-champion-2026",
            "fair_probabilities": probs,
            "observed_at": _NOW.isoformat(),
            "source": "test",
        }
    }


def test_size_entry_returns_zero_for_outright_with_default_budget() -> None:
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    sizing = strategy.size_entry(_ctx(_outright_market()))
    assert sizing.reason == "outright_budget_zero"
    assert sizing.allocation_plan.total_budget_usdc == Decimal("0")


def test_size_entry_falls_back_when_kelly_params_missing() -> None:
    """Kelly 参数缺失（EntryPlanner 未注入）时退化到平坦预算，记录 warning。"""
    config = replace(
        CurrentStrategyConfig(),
        tail_outright_budget_usdc=Decimal("100"),
        tail_outright_max_per_market_usdc=Decimal("25"),
    )
    strategy = CurrentStrategy(config=config)
    sizing = strategy.size_entry(_ctx(_outright_market()))  # no with_kelly
    assert sizing.reason == "outright_sizing_no_kelly_params"
    assert sizing.metadata.get("kelly_path") == "not_applied"


def test_size_entry_uses_kelly_path_when_params_present() -> None:
    """提供 Kelly 参数时走 kelly_plan 路径，reason 为 outright_kelly。"""
    config = replace(
        CurrentStrategyConfig(),
        tail_outright_budget_usdc=Decimal("100"),
        tail_outright_max_per_market_usdc=Decimal("25"),
    )
    strategy = CurrentStrategy(config=config)
    # 无 season odds → prob_provider 返回 conf=0 → Kelly 对所有 tokens 分配 0
    sizing = strategy.size_entry(_ctx(_outright_market(), with_kelly=True))
    assert sizing.reason == "outright_kelly"


def test_size_entry_caps_outright_at_per_market_limit() -> None:
    """赛季赔率有效时，Kelly 分配额受 per_market_usdc 上限约束。"""
    config = replace(
        CurrentStrategyConfig(),
        tail_outright_budget_usdc=Decimal("100"),
        tail_outright_max_per_market_usdc=Decimal("25"),
    )
    strategy = CurrentStrategy(config=config)
    meta = _season_odds_meta({"Yes": "0.60", "No": "0.40"})
    sizing = strategy.size_entry(_ctx(_outright_market(), with_kelly=True, metadata=meta))
    assert sizing.reason == "outright_kelly"
    for alloc in sizing.allocation_plan.allocations:
        assert alloc.buy_budget_usdc <= Decimal("25"), (
            f"token {alloc.token_id} allocation {alloc.buy_budget_usdc} exceeds per-market cap"
        )
