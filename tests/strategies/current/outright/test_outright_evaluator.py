from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_trader.domain.sports_season import SeasonOddsSnapshot
from strategies.current.outright.evaluator import evaluate_outright_opportunity
from strategies.current.outright.types import (
    OutrightAction,
    OutrightRejectReason,
)
from strategies.current.tail.types import ExecutionPermission

_NOW = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)


def _snapshot(observed_at: datetime, probs: dict[str, str]) -> SeasonOddsSnapshot:
    return SeasonOddsSnapshot(
        market_key="nba-championship",
        fair_probabilities={k: Decimal(v) for k, v in probs.items()},
        observed_at=observed_at,
        source="theoddsapi",
    )


def test_evaluator_accepts_market_with_sufficient_edge() -> None:
    snap = _snapshot(_NOW, {"Celtics": "0.40"})
    result = evaluate_outright_opportunity(
        snapshot=snap,
        market_slug="nba-championship-2026-celtics",
        condition_id="cond-1",
        outcome_label="Celtics",
        token_id="tok-celtics",
        best_ask=Decimal("0.30"),  # ~25% edge
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
    assert result.action == OutrightAction.AUTO_EXECUTE
    assert result.fair_value == Decimal("0.40")
    assert result.entry_price_cap == Decimal("0.380")
    assert result.exit_price_target == Decimal("0.43")


def test_evaluator_rejects_when_snapshot_missing() -> None:
    result = evaluate_outright_opportunity(
        snapshot=None,
        market_slug="m",
        condition_id="c",
        outcome_label="X",
        token_id="t",
        best_ask=Decimal("0.50"),
        buyable_liquidity_usdc=Decimal("100"),
        now=_NOW,
        max_season_odds_age_seconds=14400,
        min_edge_bps=500,
        max_entry_price=Decimal("0.85"),
        min_orderbook_depth_usdc=Decimal("50"),
        exit_edge_target=Decimal("0.03"),
        min_profit_per_share=Decimal("0.02"),
        execution_permission=ExecutionPermission.AUTO_EXECUTE,
    )
    assert result.accepted is False
    assert result.reject_reason == OutrightRejectReason.MISSING_SEASON_ODDS


def test_evaluator_rejects_stale_snapshot() -> None:
    stale = _NOW - timedelta(hours=5)
    snap = _snapshot(stale, {"X": "0.40"})
    result = evaluate_outright_opportunity(
        snapshot=snap,
        market_slug="m",
        condition_id="c",
        outcome_label="X",
        token_id="t",
        best_ask=Decimal("0.30"),
        buyable_liquidity_usdc=Decimal("200"),
        now=_NOW,
        max_season_odds_age_seconds=14400,  # 4h
        min_edge_bps=500,
        max_entry_price=Decimal("0.85"),
        min_orderbook_depth_usdc=Decimal("50"),
        exit_edge_target=Decimal("0.03"),
        min_profit_per_share=Decimal("0.02"),
        execution_permission=ExecutionPermission.AUTO_EXECUTE,
    )
    assert result.accepted is False
    assert result.reject_reason == OutrightRejectReason.STALE_SEASON_ODDS


def test_evaluator_rejects_insufficient_edge() -> None:
    snap = _snapshot(_NOW, {"X": "0.40"})
    result = evaluate_outright_opportunity(
        snapshot=snap,
        market_slug="m",
        condition_id="c",
        outcome_label="X",
        token_id="t",
        best_ask=Decimal("0.39"),  # only 2.5% below fair, not enough vs 5% required
        buyable_liquidity_usdc=Decimal("200"),
        now=_NOW,
        max_season_odds_age_seconds=14400,
        min_edge_bps=500,
        max_entry_price=Decimal("0.85"),
        min_orderbook_depth_usdc=Decimal("50"),
        exit_edge_target=Decimal("0.03"),
        min_profit_per_share=Decimal("0.02"),
        execution_permission=ExecutionPermission.AUTO_EXECUTE,
    )
    assert result.accepted is False
    assert result.reject_reason == OutrightRejectReason.INSUFFICIENT_EDGE


def test_evaluator_rejects_when_liquidity_below_min() -> None:
    snap = _snapshot(_NOW, {"X": "0.40"})
    result = evaluate_outright_opportunity(
        snapshot=snap,
        market_slug="m",
        condition_id="c",
        outcome_label="X",
        token_id="t",
        best_ask=Decimal("0.30"),
        buyable_liquidity_usdc=Decimal("20"),
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
    assert result.reject_reason == OutrightRejectReason.LIQUIDITY_BELOW_MIN


def test_record_only_permission_returns_record_action_when_accepted() -> None:
    snap = _snapshot(_NOW, {"X": "0.40"})
    result = evaluate_outright_opportunity(
        snapshot=snap,
        market_slug="m",
        condition_id="c",
        outcome_label="X",
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
        execution_permission=ExecutionPermission.RECORD_ONLY,
    )
    assert result.accepted is True
    assert result.action == OutrightAction.RECORD
