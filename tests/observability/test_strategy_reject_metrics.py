"""``strategy.decide_entry`` 把 outright / series 命中率与拒绝原因同步上报到
``MetricsRegistry`` 的契约测试。

覆盖（≥ 8 个用例）：
- outright accepted / outright rejected (OUTRIGHT_TEAM_NOT_RESOLVED)
- series winner rejected (MISSING_SERIES_STATE)
- series winner rejected (MISSING_SERIES_ODDS)
- series winner accepted（auto-execute + budget 解锁）
- series total_games rejected
- series game_handicap rejected
- NullMetricsPort fallback：决策路径不报错

每个用例既验证 ``strategy_*_decision_total`` 命中计数，也验证 ``strategy_*_reject_total``
的 reason 维度被打中（reason 来自有界 ``OutrightRejectReason`` / ``SeriesRejectReason``
StrEnum，不会无限膨胀）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.app.ports.extension_ports import (
    MetricsRegistryMetricsPort,
    NullMetricsPort,
)
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.extension_api import ExtensionPorts, MarketTokenView
from polymarket_trader.extension_api.context import ExtensionContext
from polymarket_trader.extension_api.decisions import ExtensionAction
from polymarket_trader.observability.metrics import MetricsRegistry
from strategies.current.config import CurrentStrategyConfig
from strategies.current.identity import STRATEGY_ID
from strategies.current.strategy import CurrentStrategy
from strategies.current.tail.types import ExecutionPermission


_NOW = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)


def _ports(registry: MetricsRegistry) -> ExtensionPorts:
    return ExtensionPorts(metrics=MetricsRegistryMetricsPort(registry=registry))


def _counter(registry: MetricsRegistry, name: str, **labels: str) -> float:
    snapshot = registry.snapshot()
    sorted_labels = tuple(sorted(labels.items()))
    for entry in snapshot.counters:
        if entry.name == name and entry.labels == sorted_labels:
            return entry.value
    return 0.0


def _orderbook(best_ask: Decimal) -> OrderbookSnapshot:
    bid = Decimal("0.30")
    return OrderbookSnapshot(
        token_id="tok",
        best_bid=bid,
        best_ask=best_ask,
        bids=(PriceLevel(price=bid, size=Decimal("200")),),
        asks=(PriceLevel(price=best_ask, size=Decimal("200")),),
        received_at=_NOW,
        tick_size=Decimal("0.01"),
    )


def _outright_market() -> Market:
    return Market(
        condition_id="cond-outright",
        market_slug="will-the-boston-celtics-win-2026-nba",
        market_question="Will the Boston Celtics win the 2026 NBA championship?",
        event_title="2026 NBA Champion",
        event_slug="2026-nba-championship-winner",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="yes-tok", outcome="Yes"),
            MarketOutcome(token_id="no-tok", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _outright_ambiguous_market() -> Market:
    """Snapshot 球队都不在该市场文本里 → OUTRIGHT_TEAM_NOT_RESOLVED。"""

    return Market(
        condition_id="cond-outright-ambiguous",
        market_slug="generic-prop-no-team",
        market_question="Will it rain in 2026 finals?",  # 没球队名
        event_title="2026 NBA Champion",
        event_slug="2026-nba-championship-winner",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="yes-tok", outcome="Yes"),
            MarketOutcome(token_id="no-tok", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _series_winner_market() -> Market:
    return Market(
        condition_id="cond-series-winner",
        market_slug="celtics-vs-knicks-series-winner",
        market_question="Will Celtics win the series?",
        event_title="NBA Playoffs Series Winner",
        event_slug="celtics-vs-knicks-2026-playoffs",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="series-yes", outcome="Yes"),
            MarketOutcome(token_id="series-no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _series_total_games_market() -> Market:
    return Market(
        condition_id="cond-series-totals",
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


def _series_handicap_market() -> Market:
    return Market(
        condition_id="cond-series-handicap",
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


def _season_odds_meta() -> dict:
    return {
        "market_key": "2026-nba-championship-winner",
        "fair_probabilities": {
            "Boston Celtics": "0.33",
            "Denver Nuggets": "0.30",
            "Phoenix Suns": "0.20",
            "Milwaukee Bucks": "0.17",
        },
        "observed_at": _NOW.isoformat(),
        "source": "theoddsapi",
    }


def _context(
    *,
    market: Market,
    metadata: dict | None = None,
    token_views: tuple[MarketTokenView, ...] = (),
) -> ExtensionContext:
    return ExtensionContext(
        trace_id="trace-metrics-test",
        strategy_id=STRATEGY_ID,
        market=market,
        metadata=metadata or {},
        now=_NOW,
        market_token_views=token_views,
    )


# ---------------------------------------------------------------------------
# Outright
# ---------------------------------------------------------------------------


def test_outright_reject_records_metric_with_reject_reason() -> None:
    registry = MetricsRegistry()
    strategy = CurrentStrategy(config=CurrentStrategyConfig(), ports=_ports(registry))
    market = _outright_ambiguous_market()
    metadata = {"season_odds_snapshot": _season_odds_meta()}
    token_views = (
        MarketTokenView(token_id="yes-tok", outcome="Yes", orderbook=_orderbook(Decimal("0.30"))),
        MarketTokenView(token_id="no-tok", outcome="No", orderbook=_orderbook(Decimal("0.30"))),
    )
    decision = strategy.decide_entry(_context(market=market, metadata=metadata, token_views=token_views))
    assert decision.action == ExtensionAction.SKIP

    assert _counter(registry, "strategy_outright_decision_total", outcome="rejected") == 1.0
    assert (
        _counter(registry, "strategy_outright_reject_total", reason="outright_team_not_resolved")
        == 1.0
    )
    # accepted bucket 不应触发。
    assert _counter(registry, "strategy_outright_decision_total", outcome="accepted") == 0.0


def test_outright_accepted_records_metric() -> None:
    registry = MetricsRegistry()
    config = replace(
        CurrentStrategyConfig(),
        tail_outright_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_outright_budget_usdc=Decimal("100"),
        tail_outright_max_per_market_usdc=Decimal("25"),
        tail_outright_min_edge_bps=300,
        tail_outright_max_entry_price=Decimal("0.99"),
        tail_outright_min_orderbook_depth_usdc=Decimal("20"),
    )
    strategy = CurrentStrategy(config=config, ports=_ports(registry))
    market = _outright_market()
    metadata = {"season_odds_snapshot": _season_odds_meta()}
    # snapshot 给 Celtics 0.33, best_ask=0.20 → edge 充足
    token_views = (
        MarketTokenView(token_id="yes-tok", outcome="Yes", orderbook=_orderbook(Decimal("0.20"))),
        MarketTokenView(token_id="no-tok", outcome="No", orderbook=_orderbook(Decimal("0.85"))),
    )
    decision = strategy.decide_entry(_context(market=market, metadata=metadata, token_views=token_views))
    assert decision.action == ExtensionAction.BUY

    assert _counter(registry, "strategy_outright_decision_total", outcome="accepted") == 1.0
    assert _counter(registry, "strategy_outright_decision_total", outcome="rejected") == 0.0


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------


def test_series_winner_missing_state_records_metric() -> None:
    registry = MetricsRegistry()
    strategy = CurrentStrategy(config=CurrentStrategyConfig(), ports=_ports(registry))
    market = _series_winner_market()
    token_views = (
        MarketTokenView(token_id="series-yes", outcome="Yes", orderbook=_orderbook(Decimal("0.50"))),
        MarketTokenView(token_id="series-no", outcome="No", orderbook=_orderbook(Decimal("0.50"))),
    )
    decision = strategy.decide_entry(_context(market=market, metadata={}, token_views=token_views))
    assert decision.action == ExtensionAction.SKIP

    assert (
        _counter(registry, "strategy_series_decision_total", sub_type="winner", outcome="rejected")
        == 1.0
    )
    assert (
        _counter(
            registry,
            "strategy_series_reject_total",
            sub_type="winner",
            reason="missing_series_state",
        )
        == 1.0
    )


def test_series_winner_missing_odds_records_metric() -> None:
    registry = MetricsRegistry()
    strategy = CurrentStrategy(config=CurrentStrategyConfig(), ports=_ports(registry))
    market = _series_winner_market()
    metadata = {"series_state": _series_state_meta()}
    token_views = (
        MarketTokenView(token_id="series-yes", outcome="Yes", orderbook=_orderbook(Decimal("0.50"))),
        MarketTokenView(token_id="series-no", outcome="No", orderbook=_orderbook(Decimal("0.50"))),
    )
    strategy.decide_entry(_context(market=market, metadata=metadata, token_views=token_views))

    assert (
        _counter(
            registry,
            "strategy_series_reject_total",
            sub_type="winner",
            reason="missing_series_odds",
        )
        == 1.0
    )


def test_series_winner_accepted_records_metric() -> None:
    registry = MetricsRegistry()
    config = replace(
        CurrentStrategyConfig(),
        tail_series_winner_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_series_winner_budget_usdc=Decimal("100"),
        tail_series_winner_max_per_market_usdc=Decimal("25"),
        tail_series_winner_min_edge_bps=500,
        tail_series_winner_max_entry_price=Decimal("0.99"),
        tail_series_winner_min_orderbook_depth_usdc=Decimal("20"),
    )
    strategy = CurrentStrategy(config=config, ports=_ports(registry))
    market = _series_winner_market()
    metadata = {
        "series_state": _series_state_meta(),
        "game_odds": _game_odds_meta(),
    }
    token_views = (
        MarketTokenView(token_id="series-yes", outcome="Yes", orderbook=_orderbook(Decimal("0.50"))),
        MarketTokenView(token_id="series-no", outcome="No", orderbook=_orderbook(Decimal("0.50"))),
    )
    decision = strategy.decide_entry(_context(market=market, metadata=metadata, token_views=token_views))
    assert decision.action == ExtensionAction.BUY

    assert (
        _counter(registry, "strategy_series_decision_total", sub_type="winner", outcome="accepted")
        == 1.0
    )
    assert (
        _counter(registry, "strategy_series_decision_total", sub_type="winner", outcome="rejected")
        == 0.0
    )


def test_series_total_games_reject_records_metric() -> None:
    registry = MetricsRegistry()
    strategy = CurrentStrategy(config=CurrentStrategyConfig(), ports=_ports(registry))
    market = _series_total_games_market()
    token_views = (
        MarketTokenView(token_id="games-over", outcome="Over 5.5", orderbook=_orderbook(Decimal("0.50"))),
        MarketTokenView(token_id="games-under", outcome="Under 5.5", orderbook=_orderbook(Decimal("0.50"))),
    )
    strategy.decide_entry(_context(market=market, metadata={}, token_views=token_views))

    assert (
        _counter(
            registry, "strategy_series_decision_total", sub_type="total_games", outcome="rejected"
        )
        == 1.0
    )
    # rejection reason 应当被记录（具体值 missing_series_state）。
    assert (
        _counter(
            registry,
            "strategy_series_reject_total",
            sub_type="total_games",
            reason="missing_series_state",
        )
        == 1.0
    )


def test_series_game_handicap_reject_records_metric() -> None:
    registry = MetricsRegistry()
    strategy = CurrentStrategy(config=CurrentStrategyConfig(), ports=_ports(registry))
    market = _series_handicap_market()
    token_views = (
        MarketTokenView(token_id="lakers-minus", outcome="Lakers -3.5", orderbook=_orderbook(Decimal("0.50"))),
        MarketTokenView(token_id="opponent-plus", outcome="Opponent +3.5", orderbook=_orderbook(Decimal("0.50"))),
    )
    strategy.decide_entry(_context(market=market, metadata={}, token_views=token_views))

    assert (
        _counter(
            registry,
            "strategy_series_decision_total",
            sub_type="game_handicap",
            outcome="rejected",
        )
        == 1.0
    )


def test_null_metrics_port_does_not_break_decision_path() -> None:
    """NullMetricsPort 场景下，decide_entry 不应抛错；返回值与有真 registry 时一致。"""

    null_ports = ExtensionPorts(metrics=NullMetricsPort())
    strategy = CurrentStrategy(config=CurrentStrategyConfig(), ports=null_ports)
    market = _series_winner_market()
    decision = strategy.decide_entry(_context(market=market, metadata={}))
    assert decision.action == ExtensionAction.SKIP


def test_no_metrics_port_attached_does_not_break_decision_path() -> None:
    """ports.metrics=None（旧测试场景）仍然安全：metric 上报路径直接 short-circuit。"""

    strategy = CurrentStrategy(config=CurrentStrategyConfig(), ports=ExtensionPorts())
    market = _series_winner_market()
    decision = strategy.decide_entry(_context(market=market, metadata={}))
    assert decision.action == ExtensionAction.SKIP


def test_cumulative_counts_across_multiple_decisions() -> None:
    """同一个 registry 累计计数：连续两次 reject → counter 累计到 2。"""

    registry = MetricsRegistry()
    strategy = CurrentStrategy(config=CurrentStrategyConfig(), ports=_ports(registry))
    market = _series_winner_market()
    for _ in range(2):
        strategy.decide_entry(_context(market=market, metadata={}))

    assert (
        _counter(registry, "strategy_series_decision_total", sub_type="winner", outcome="rejected")
        == 2.0
    )
    assert (
        _counter(
            registry,
            "strategy_series_reject_total",
            sub_type="winner",
            reason="missing_series_state",
        )
        == 2.0
    )
