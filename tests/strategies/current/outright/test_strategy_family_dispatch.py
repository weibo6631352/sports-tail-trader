from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.extension_api.context import ExtensionContext
from polymarket_trader.extension_api.decisions import ExtensionAction, MarketTokenView
from strategies.current.config import CurrentStrategyConfig
from strategies.current.identity import STRATEGY_ID
from strategies.current.strategy import CurrentStrategy
from strategies.current.tail.types import ExecutionPermission


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
            MarketOutcome(token_id="celtics-yes", outcome="Yes"),
            MarketOutcome(token_id="celtics-no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _context(market: Market, *, metadata: dict | None = None) -> ExtensionContext:
    return ExtensionContext(
        trace_id="test",
        strategy_id=STRATEGY_ID,
        market=market,
        metadata=metadata or {},
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )


def test_outright_decide_entry_returns_skip_with_outright_metadata() -> None:
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    market = _outright_market()
    ctx = _context(market)

    decision = strategy.decide_entry(ctx)

    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("market_family") == "outright"
    # 没有 season_odds_snapshot → 评估器拒绝原因是 missing_season_odds
    assert decision.metadata.get("outright_reject_reason") == "missing_season_odds"


def test_outright_decide_entry_reports_stale_when_snapshot_too_old() -> None:
    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    market = _outright_market()
    stale_snapshot = {
        "market_key": "nba-champion-2026",
        "fair_probabilities": {"Yes": "0.30"},
        # observed 比 context.now 早 5 小时（默认 max_season_odds_age = 4h = 14400s）
        "observed_at": "2026-05-10T19:00:00+00:00",
        "source": "theoddsapi",
    }
    ctx = _context(
        market,
        metadata={"season_odds_snapshot": stale_snapshot},
    )

    decision = strategy.decide_entry(ctx)

    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("market_family") == "outright"
    assert decision.metadata.get("outright_reject_reason") == "stale_season_odds"


def test_outright_match_live_state_returns_none() -> None:
    """outright 不参与 single-game live state 匹配；hook 应返回 None。"""

    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    match = strategy.match_live_state(_outright_market(), events=())
    assert match is None


def _orderbook(token_id: str, *, best_ask: Decimal, depth_shares: Decimal) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id=token_id,
        best_bid=best_ask - Decimal("0.01"),
        best_ask=best_ask,
        bids=(),
        asks=(PriceLevel(price=best_ask, size=depth_shares),),
        received_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )


def test_outright_decide_entry_builds_buy_when_unlocked_and_edge_sufficient() -> None:
    config = replace(
        CurrentStrategyConfig(),
        tail_outright_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_outright_budget_usdc=Decimal("50"),
    )
    strategy = CurrentStrategy(config=config)
    market = _outright_market()
    yes_book = _orderbook("celtics-yes", best_ask=Decimal("0.30"), depth_shares=Decimal("1000"))
    no_book = _orderbook("celtics-no", best_ask=Decimal("0.80"), depth_shares=Decimal("500"))
    snapshot_payload = {
        "market_key": "will-celtics-win-2026-nba-championship",
        "fair_probabilities": {"Yes": "0.40", "No": "0.60"},
        "observed_at": "2026-05-11T11:00:00+00:00",
        "source": "theoddsapi",
    }
    ctx = ExtensionContext(
        trace_id="t",
        strategy_id=STRATEGY_ID,
        market=market,
        market_token_views=(
            MarketTokenView(token_id="celtics-yes", outcome="Yes", orderbook=yes_book),
            MarketTokenView(token_id="celtics-no", outcome="No", orderbook=no_book),
        ),
        metadata={"season_odds_snapshot": snapshot_payload},
        now=datetime(2026, 5, 11, 12, tzinfo=timezone.utc),
    )

    decision = strategy.decide_entry(ctx)

    assert decision.action == ExtensionAction.BUY
    assert decision.token_id == "celtics-yes"
    assert decision.amount_usdc == Decimal("25")  # min(budget 50, per-market cap 25)
    assert decision.price <= Decimal("0.40")  # 入场价 ≤ fair value
    assert decision.metadata.get("market_family") == "outright"


def test_outright_decide_entry_rejected_when_market_end_passed() -> None:
    """风控前置：market.end_date 已过去，不应构造 BUY，应返回带 reject reason 的 SKIP。"""

    from datetime import timedelta

    config = replace(
        CurrentStrategyConfig(),
        tail_outright_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_outright_budget_usdc=Decimal("50"),
    )
    strategy = CurrentStrategy(config=config)
    base = _outright_market()
    expired = Market(
        condition_id=base.condition_id,
        market_slug=base.market_slug,
        market_question=base.market_question,
        event_title=base.event_title,
        event_slug=base.event_slug,
        category=base.category,
        tags=base.tags,
        outcomes=base.outcomes,
        end_date=datetime(2026, 5, 10, tzinfo=timezone.utc),
        trading_status=TradingStatus.ELIGIBLE,
    )
    yes_book = _orderbook("celtics-yes", best_ask=Decimal("0.30"), depth_shares=Decimal("1000"))
    snapshot = {
        "market_key": "will-celtics-win-2026-nba-championship",
        "fair_probabilities": {"Yes": "0.40"},
        "observed_at": "2026-05-11T11:00:00+00:00",
        "source": "theoddsapi",
    }
    ctx = ExtensionContext(
        trace_id="t",
        strategy_id=STRATEGY_ID,
        market=expired,
        market_token_views=(MarketTokenView(token_id="celtics-yes", outcome="Yes", orderbook=yes_book),),
        metadata={"season_odds_snapshot": snapshot},
        now=datetime(2026, 5, 11, tzinfo=timezone.utc) + timedelta(seconds=0),
    )

    decision = strategy.decide_entry(ctx)
    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("outright_reject_reason") == "market_end_passed"


def test_outright_decide_entry_rejected_when_total_exposure_exceeds_cap() -> None:
    """已累计敞口接近 budget 上限时 risk gate 拒绝新单。"""

    config = replace(
        CurrentStrategyConfig(),
        tail_outright_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_outright_budget_usdc=Decimal("30"),  # 已敞口 25 + proposed 25 > 30
        tail_outright_max_per_market_usdc=Decimal("25"),
    )
    strategy = CurrentStrategy(config=config)
    market = _outright_market()
    yes_book = _orderbook("celtics-yes", best_ask=Decimal("0.30"), depth_shares=Decimal("1000"))
    snapshot = {
        "market_key": "will-celtics-win-2026-nba-championship",
        "fair_probabilities": {"Yes": "0.40"},
        "observed_at": "2026-05-11T11:00:00+00:00",
        "source": "theoddsapi",
    }
    ctx = ExtensionContext(
        trace_id="t",
        strategy_id=STRATEGY_ID,
        market=market,
        market_token_views=(MarketTokenView(token_id="celtics-yes", outcome="Yes", orderbook=yes_book),),
        metadata={
            "season_odds_snapshot": snapshot,
            "outright_total_exposure_usdc": "25",
        },
        now=datetime(2026, 5, 11, 12, tzinfo=timezone.utc),
    )

    decision = strategy.decide_entry(ctx)
    assert decision.action == ExtensionAction.SKIP
    # 已敞口 25 + proposed 25 > total budget 30 → 总额耗尽
    assert decision.metadata.get("outright_reject_reason") == "total_budget_exhausted"


def test_outright_decide_entry_locked_when_budget_zero() -> None:
    """budget=0 时即使 permission=AUTO_EXECUTE 也只录单不构造 BUY。"""

    config = replace(
        CurrentStrategyConfig(),
        tail_outright_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_outright_budget_usdc=Decimal("0"),
    )
    strategy = CurrentStrategy(config=config)
    market = _outright_market()
    yes_book = _orderbook("celtics-yes", best_ask=Decimal("0.30"), depth_shares=Decimal("1000"))
    snapshot_payload = {
        "market_key": "will-celtics-win-2026-nba-championship",
        "fair_probabilities": {"Yes": "0.40"},
        "observed_at": "2026-05-11T11:00:00+00:00",
        "source": "theoddsapi",
    }
    ctx = ExtensionContext(
        trace_id="t",
        strategy_id=STRATEGY_ID,
        market=market,
        market_token_views=(
            MarketTokenView(token_id="celtics-yes", outcome="Yes", orderbook=yes_book),
        ),
        metadata={"season_odds_snapshot": snapshot_payload},
        now=datetime(2026, 5, 11, 12, tzinfo=timezone.utc),
    )

    decision = strategy.decide_entry(ctx)

    assert decision.action == ExtensionAction.SKIP
    assert decision.metadata.get("market_family") == "outright"
    # evaluator 接受了，但 budget=0 锁住执行；策略侧降级为 record。
    assert decision.metadata.get("outright_action") == "record"
    assert decision.metadata.get("outright_accepted") is True
    assert decision.metadata.get("budget_unlocked") is False
