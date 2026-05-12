from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.domain.sports_season import SeasonOddsSnapshot
from strategies.current._shared.edge_gates import entry_price_cap
from strategies.current.outright.pricing import (
    outright_exit_price_target,
    outright_fair_value,
)


def _snapshot(probs: dict[str, str]) -> SeasonOddsSnapshot:
    return SeasonOddsSnapshot(
        market_key="m",
        fair_probabilities={k: Decimal(v) for k, v in probs.items()},
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
        source="theoddsapi",
    )


def _market() -> Market:
    return Market(
        condition_id="cond",
        market_slug="m",
        outcomes=(MarketOutcome(token_id="t", outcome="Celtics"),),
    )


def test_fair_value_returns_probability() -> None:
    snap = _snapshot({"Celtics": "0.45"})
    result = outright_fair_value(snap, _market(), "Celtics")
    assert result.value == Decimal("0.45")
    assert result.reject is None


def test_fair_value_returns_none_when_outcome_missing() -> None:
    snap = _snapshot({"Celtics": "0.45"})
    result = outright_fair_value(snap, _market(), "Lakers")
    assert result.value is None
    # 非 YES/NO 且 snapshot 无此 key → 旧契约：ODDS_OUTCOME_NOT_MAPPED
    from strategies.current.outright.types import OutrightRejectReason
    assert result.reject == OutrightRejectReason.ODDS_OUTCOME_NOT_MAPPED


def test_entry_price_cap_subtracts_edge() -> None:
    cap = entry_price_cap(
        Decimal("0.40"),
        min_edge_bps=500,  # 5%
        max_entry_price=Decimal("0.85"),
    )
    # 0.40 * (1 - 0.05) = 0.38
    assert cap == Decimal("0.380")


def test_entry_price_cap_respects_strategy_ceiling() -> None:
    cap = entry_price_cap(
        Decimal("0.95"),
        min_edge_bps=100,
        max_entry_price=Decimal("0.85"),
    )
    assert cap == Decimal("0.85")


def test_exit_target_above_fair_and_above_entry() -> None:
    target = outright_exit_price_target(
        Decimal("0.40"),
        Decimal("0.30"),
        exit_edge_target=Decimal("0.03"),
        min_profit_per_share=Decimal("0.02"),
    )
    # max(0.40+0.03, 0.30+0.02) = 0.43
    assert target == Decimal("0.43")


def test_exit_target_clamps_to_ceiling() -> None:
    target = outright_exit_price_target(
        Decimal("0.99"),
        Decimal("0.50"),
        exit_edge_target=Decimal("0.10"),
        min_profit_per_share=Decimal("0.01"),
    )
    assert target == Decimal("0.99")
