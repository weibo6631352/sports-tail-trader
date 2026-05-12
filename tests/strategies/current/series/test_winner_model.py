"""series_win_probability 负二项分布求和的数值正确性。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from strategies.current.series.types import SeriesState
from strategies.current.series.winner_model import (
    series_win_probability,
    team_b_win_probability,
)


_NOW = datetime(2026, 5, 13, tzinfo=timezone.utc)


def _state(wins_a: int, wins_b: int, best_of: int = 7) -> SeriesState:
    return SeriesState(
        team_a="A",
        team_b="B",
        wins_a=wins_a,
        wins_b=wins_b,
        best_of=best_of,
        next_game_at=None,
        observed_at=_NOW,
    )


def test_balanced_zero_zero_p_half_returns_half() -> None:
    p = series_win_probability(_state(0, 0), Decimal("0.5"))
    assert p == Decimal("0.5")


def test_zero_zero_best_of_seven_p_zero_point_six() -> None:
    # 0-0, p=0.6, best_of=7 → 直算 P(team A 拿 4 场前 B 拿不到 4 场) ≈ 0.71
    p = series_win_probability(_state(0, 0), Decimal("0.6"))
    # 解析解：Σ_{i=0..3} C(3+i, i) * 0.6^4 * 0.4^i = 0.6^4 (1 + 4*0.4 + 10*0.16 + 20*0.064)
    # = 0.1296 * (1 + 1.6 + 1.6 + 1.28) = 0.1296 * 5.48 = 0.710208
    assert abs(p - Decimal("0.710208")) < Decimal("0.0001")


def test_three_zero_balanced_p_close_to_seven_eighths() -> None:
    # 3-0 lead, p=0.5, best_of=7 → needed_a=1, needed_b=4
    # P = Σ_{i=0..3} C(0+i, i) * 0.5^1 * 0.5^i = 0.5 * (1 + 0.5 + 0.25 + 0.125) = 0.5 * 1.875
    # = 0.9375
    p = series_win_probability(_state(3, 0), Decimal("0.5"))
    assert p == Decimal("0.9375")


def test_team_a_already_clinched_returns_one() -> None:
    p = series_win_probability(_state(4, 0), Decimal("0.3"))
    assert p == Decimal(1)


def test_team_b_already_clinched_returns_zero() -> None:
    p = series_win_probability(_state(0, 4), Decimal("0.9"))
    assert p == Decimal(0)


def test_best_of_five_one_zero_p_half() -> None:
    # best_of=5, 1-0, p=0.5 → needed_a=2, needed_b=3
    # P = Σ_{i=0..2} C(1+i, i) * 0.5^2 * 0.5^i
    # = 0.25 * (1 + 2*0.5 + 3*0.25) = 0.25 * (1 + 1 + 0.75) = 0.25 * 2.75 = 0.6875
    p = series_win_probability(_state(1, 0, best_of=5), Decimal("0.5"))
    assert p == Decimal("0.6875")


def test_team_b_win_probability_is_complement() -> None:
    state = _state(2, 1)
    p_a = series_win_probability(state, Decimal("0.55"))
    p_b = team_b_win_probability(state, Decimal("0.55"))
    assert p_a + p_b == Decimal(1)


def test_extreme_p_zero_returns_zero() -> None:
    p = series_win_probability(_state(0, 0), Decimal("0"))
    assert p == Decimal(0)


def test_extreme_p_one_returns_one() -> None:
    p = series_win_probability(_state(0, 0), Decimal("1"))
    assert p == Decimal(1)
