"""二元 YES/NO 冠军市场的 pricing 与 evaluator 集成测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.domain.sports_season import SeasonOddsSnapshot
from strategies.current.outright.evaluator import evaluate_outright_opportunity
from strategies.current.outright.pricing import outright_fair_value
from strategies.current.outright.types import OutrightRejectReason
from strategies.current.tail.types import ExecutionPermission

_NOW = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)


def _snapshot(probs: dict[str, str], observed_at: datetime = _NOW) -> SeasonOddsSnapshot:
    return SeasonOddsSnapshot(
        market_key="nba-championship",
        fair_probabilities={k: Decimal(v) for k, v in probs.items()},
        observed_at=observed_at,
        source="theoddsapi",
    )


def _celtics_market() -> Market:
    return Market(
        condition_id="cond-celtics",
        market_slug="will-the-boston-celtics-win-2026-nba",
        outcomes=(
            MarketOutcome(token_id="yes-tok", outcome="Yes"),
            MarketOutcome(token_id="no-tok", outcome="No"),
        ),
        market_question="Will the Boston Celtics win the 2026 NBA championship?",
        event_title="2026 NBA Champion",
        event_slug="2026-nba-championship-winner",
    )


def _well_formed_snapshot() -> SeasonOddsSnapshot:
    # Σp = 0.33 + 0.30 + 0.20 + 0.17 = 1.0 in [0.95, 1.05]
    return _snapshot(
        {
            "Boston Celtics": "0.33",
            "Denver Nuggets": "0.30",
            "Phoenix Suns": "0.20",
            "Milwaukee Bucks": "0.17",
        }
    )


def test_binary_yes_returns_team_probability() -> None:
    market = _celtics_market()
    snap = _well_formed_snapshot()
    result = outright_fair_value(snap, market, "Yes")
    assert result.value == Decimal("0.33")
    assert result.reject is None


def test_binary_no_returns_one_minus_team_probability() -> None:
    market = _celtics_market()
    snap = _well_formed_snapshot()
    result = outright_fair_value(snap, market, "No")
    assert result.value == Decimal("0.67")


def test_binary_team_not_resolvable_returns_typed_reject() -> None:
    # market 不含 snapshot 中任何球队 → OUTRIGHT_TEAM_NOT_RESOLVED
    market = Market(
        condition_id="c",
        market_slug="m",
        outcomes=(MarketOutcome(token_id="t", outcome="Yes"),),
        market_question="Will the Phoenix Suns win 2026 NBA championship?",
    )
    snap = _snapshot({"Boston Celtics": "0.5", "Denver Nuggets": "0.5"})
    result = outright_fair_value(snap, market, "Yes")
    assert result.value is None
    assert result.reject == OutrightRejectReason.OUTRIGHT_TEAM_NOT_RESOLVED


def test_binary_incomplete_snapshot_returns_typed_reject() -> None:
    market = _celtics_market()
    snap = _snapshot({"Boston Celtics": "0.30", "Denver Nuggets": "0.30"})  # Σ=0.6
    result = outright_fair_value(snap, market, "Yes")
    assert result.value is None
    assert result.reject == OutrightRejectReason.SEASON_ODDS_INCOMPLETE


def test_categorical_outcome_still_direct_lookup() -> None:
    # 类别型 outright：outcome 是球队名 → 直查不依赖 team_resolver。
    market = Market(
        condition_id="c",
        market_slug="m",
        outcomes=(MarketOutcome(token_id="t", outcome="Boston Celtics"),),
    )
    snap = _snapshot({"Boston Celtics": "0.45"})
    result = outright_fair_value(snap, market, "Boston Celtics")
    assert result.value == Decimal("0.45")
    assert result.reject is None


def test_evaluator_propagates_outright_team_not_resolved() -> None:
    market = Market(
        condition_id="c",
        market_slug="m",
        outcomes=(MarketOutcome(token_id="t", outcome="Yes"),),
        market_question="Will the Phoenix Suns win 2026 NBA championship?",
    )
    snap = _snapshot({"Boston Celtics": "0.5", "Denver Nuggets": "0.5"})
    result = evaluate_outright_opportunity(
        snapshot=snap,
        market=market,
        outcome_label="Yes",
        token_id="t",
        best_ask=Decimal("0.30"),
        buyable_liquidity_usdc=Decimal("500"),
        now=_NOW,
        max_season_odds_age_seconds=14400,
        min_edge_bps=500,
        max_entry_price=Decimal("0.85"),
        min_orderbook_depth_usdc=Decimal("100"),
        exit_edge_target=Decimal("0.03"),
        min_profit_per_share=Decimal("0.02"),
        execution_permission=ExecutionPermission.AUTO_EXECUTE,
    )
    assert result.accepted is False
    assert result.reject_reason == OutrightRejectReason.OUTRIGHT_TEAM_NOT_RESOLVED


def test_evaluator_propagates_season_odds_incomplete() -> None:
    market = _celtics_market()
    snap = _snapshot({"Boston Celtics": "0.30", "Denver Nuggets": "0.30"})  # Σ=0.6
    result = evaluate_outright_opportunity(
        snapshot=snap,
        market=market,
        outcome_label="Yes",
        token_id="yes-tok",
        best_ask=Decimal("0.30"),
        buyable_liquidity_usdc=Decimal("500"),
        now=_NOW,
        max_season_odds_age_seconds=14400,
        min_edge_bps=500,
        max_entry_price=Decimal("0.85"),
        min_orderbook_depth_usdc=Decimal("100"),
        exit_edge_target=Decimal("0.03"),
        min_profit_per_share=Decimal("0.02"),
        execution_permission=ExecutionPermission.AUTO_EXECUTE,
    )
    assert result.accepted is False
    assert result.reject_reason == OutrightRejectReason.SEASON_ODDS_INCOMPLETE


def test_evaluator_accepts_binary_yes_with_sufficient_edge() -> None:
    market = _celtics_market()
    snap = _well_formed_snapshot()  # Boston Celtics = 0.33
    result = evaluate_outright_opportunity(
        snapshot=snap,
        market=market,
        outcome_label="Yes",
        token_id="yes-tok",
        best_ask=Decimal("0.20"),  # well below fair 0.33
        buyable_liquidity_usdc=Decimal("500"),
        now=_NOW,
        max_season_odds_age_seconds=14400,
        min_edge_bps=500,
        max_entry_price=Decimal("0.85"),
        min_orderbook_depth_usdc=Decimal("100"),
        exit_edge_target=Decimal("0.03"),
        min_profit_per_share=Decimal("0.02"),
        execution_permission=ExecutionPermission.AUTO_EXECUTE,
    )
    assert result.accepted is True
    assert result.fair_value == Decimal("0.33")
