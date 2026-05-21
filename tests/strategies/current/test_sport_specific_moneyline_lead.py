"""Tests for sport-specific moneyline lead thresholds.

Soccer and hockey are low-scoring sports (typical final: 1-0, 2-1) so they use
sport-specific lead thresholds (2) instead of the global default (6).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from strategies.current.tail import (
    SportsMarketSnapshot,
    SportsMarketSide,
    SportsMarketType,
    TailRejectReason,
    evaluate_tail_opportunity,
    live_game_state_from_metadata,
)
from strategies.current.tail.types import TailPolicy
from strategies.sports_framework import is_hockey_game, is_soccer_game


def _policy(
    min_moneyline_lead: int = 6,
    soccer_min_moneyline_lead: int = 1,
    hockey_min_moneyline_lead: int = 1,
    max_moneyline_seconds_remaining: int = 180,
) -> TailPolicy:
    return TailPolicy(
        min_moneyline_lead=min_moneyline_lead,
        soccer_min_moneyline_lead=soccer_min_moneyline_lead,
        hockey_min_moneyline_lead=hockey_min_moneyline_lead,
        max_moneyline_seconds_remaining=max_moneyline_seconds_remaining,
    )


def _market(side: SportsMarketSide = SportsMarketSide.HOME) -> SportsMarketSnapshot:
    return SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=side,
        token_id="test",
        line=None,
        best_ask=Decimal("0.90"),
        buyable_liquidity_usdc=Decimal("10"),
    )


def _soccer_game(home: int, away: int, seconds_remaining: int = 120):
    return live_game_state_from_metadata({
        "league": "EPL",
        "sport": "soccer",
        "home_name": "Arsenal",
        "away_name": "Chelsea",
        "home_score": home,
        "away_score": away,
        "period": "second_half",
        "status": "live",
        "seconds_remaining": seconds_remaining,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })


def _hockey_game(home: int, away: int, seconds_remaining: int = 120):
    return live_game_state_from_metadata({
        "league": "NHL",
        "sport": "ice-hockey",
        "home_name": "Bruins",
        "away_name": "Rangers",
        "home_score": home,
        "away_score": away,
        "period": "3",
        "status": "live",
        "seconds_remaining": seconds_remaining,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })


def _basketball_game(home: int, away: int, seconds_remaining: int = 120):
    return live_game_state_from_metadata({
        "league": "NBA",
        "sport": "basketball",
        "home_name": "Lakers",
        "away_name": "Celtics",
        "home_score": home,
        "away_score": away,
        "period": "Q4",
        "status": "live",
        "seconds_remaining": seconds_remaining,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Sport detection
# ---------------------------------------------------------------------------


def test_is_soccer_game_from_sport_field() -> None:
    game = _soccer_game(1, 0)
    assert game is not None
    assert is_soccer_game(game)


def test_is_hockey_game_from_sport_field() -> None:
    game = _hockey_game(2, 1)
    assert game is not None
    assert is_hockey_game(game)


def test_basketball_is_not_soccer_or_hockey() -> None:
    game = _basketball_game(102, 94)
    assert game is not None
    assert not is_soccer_game(game)
    assert not is_hockey_game(game)


# ---------------------------------------------------------------------------
# Soccer: 1-goal lead accepted with sport-specific threshold
# ---------------------------------------------------------------------------


def test_soccer_1_goal_lead_accepted() -> None:
    game = _soccer_game(1, 0)
    evaluation = evaluate_tail_opportunity(game, _market(), policy=_policy())
    assert evaluation.accepted, f"should accept; reason={evaluation.reason}"


def test_soccer_2_goal_lead_accepted() -> None:
    game = _soccer_game(2, 0)
    evaluation = evaluate_tail_opportunity(game, _market(), policy=_policy())
    assert evaluation.accepted




def test_soccer_0_goal_lead_rejected() -> None:
    game = _soccer_game(0, 0)  # tied
    evaluation = evaluate_tail_opportunity(game, _market(), policy=_policy())
    assert not evaluation.accepted
    assert evaluation.reason == TailRejectReason.INSUFFICIENT_LEAD.value


def test_soccer_would_be_blocked_by_global_lead() -> None:
    """Confirm 1-goal lead fails the global threshold of 6."""
    game = _soccer_game(1, 0)
    policy_global = _policy(soccer_min_moneyline_lead=6)  # override to global
    evaluation = evaluate_tail_opportunity(game, _market(), policy=policy_global)
    assert not evaluation.accepted
    assert evaluation.reason == TailRejectReason.INSUFFICIENT_LEAD.value


# ---------------------------------------------------------------------------
# Hockey: 2-goal lead accepted with sport-specific threshold
# ---------------------------------------------------------------------------


def test_hockey_2_goal_lead_accepted() -> None:
    game = _hockey_game(3, 1)
    evaluation = evaluate_tail_opportunity(game, _market(), policy=_policy())
    assert evaluation.accepted


def test_hockey_1_goal_lead_accepted() -> None:
    game = _hockey_game(2, 1)  # lead=1, threshold=1
    evaluation = evaluate_tail_opportunity(game, _market(), policy=_policy())
    assert evaluation.accepted


def test_hockey_tied_rejected() -> None:
    game = _hockey_game(1, 1)  # tied, lead=0
    evaluation = evaluate_tail_opportunity(game, _market(), policy=_policy())
    assert not evaluation.accepted
    assert evaluation.reason == TailRejectReason.INSUFFICIENT_LEAD.value


def test_hockey_would_be_blocked_by_global_lead() -> None:
    """Confirm 2-goal lead fails global threshold of 6."""
    game = _hockey_game(3, 1)
    policy_global = _policy(hockey_min_moneyline_lead=6)
    evaluation = evaluate_tail_opportunity(game, _market(), policy=policy_global)
    assert not evaluation.accepted
    assert evaluation.reason == TailRejectReason.INSUFFICIENT_LEAD.value


# ---------------------------------------------------------------------------
# Basketball: global threshold still applies (6 points)
# ---------------------------------------------------------------------------


def test_basketball_6_point_lead_accepted() -> None:
    game = _basketball_game(102, 96)  # lead=6
    evaluation = evaluate_tail_opportunity(game, _market(), policy=_policy())
    assert evaluation.accepted


def test_basketball_5_point_lead_rejected() -> None:
    game = _basketball_game(101, 96)  # lead=5
    evaluation = evaluate_tail_opportunity(game, _market(), policy=_policy())
    assert not evaluation.accepted
    assert evaluation.reason == TailRejectReason.INSUFFICIENT_LEAD.value
