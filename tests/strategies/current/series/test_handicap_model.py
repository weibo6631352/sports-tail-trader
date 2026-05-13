"""handicap_model: series-scope DP + single_game-scope spread de-vig。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.infra.sports.game_odds_client import GameSpreadSnapshot
from strategies.current.series.handicap_model import (
    series_handicap_cover_probability,
    single_game_cover_probability,
)
from strategies.current.series.types import SeriesState


_NOW = datetime(2026, 5, 13, tzinfo=timezone.utc)


def _state(wins_a: int = 0, wins_b: int = 0, best_of: int = 7) -> SeriesState:
    return SeriesState(
        team_a="Boston Celtics",
        team_b="New York Knicks",
        wins_a=wins_a,
        wins_b=wins_b,
        best_of=best_of,
        next_game_at=None,
        observed_at=_NOW,
    )


def test_zero_handicap_p_half_is_half() -> None:
    # 让分 0、p=0.5 → team_a 净赢 > 0 概率 = 0.5（对称分布 team_a 平局可能性=0）。
    # 实际上 best-of-7 不允许平局，team_a 赢系列赛 ⟺ diff > 0。
    p = series_handicap_cover_probability(_state(), Decimal("0.5"), Decimal("0"))
    assert p == Decimal("0.5")


def test_handicap_minus_one_point_five_requires_two_game_margin() -> None:
    # handicap=-1.5 → team_a 必须多赢 ≥ 2 场。p=0.6, 0-0 best-of-7。
    # 对称性：series_handicap(+1.5) + series_handicap(-1.5) 不等于 1（diff 概率分布不对称），
    # 但 series_handicap(-1.5, p) + series_handicap(-1.5, q=1-p, swapped) = 1。
    p_minus = series_handicap_cover_probability(_state(), Decimal("0.6"), Decimal("-1.5"))
    p_plus = series_handicap_cover_probability(_state(), Decimal("0.6"), Decimal("1.5"))
    # -1.5 更严苛 → 概率应小于 +1.5。
    assert p_minus < p_plus
    assert Decimal(0) <= p_minus <= Decimal(1)
    assert Decimal(0) <= p_plus <= Decimal(1)


def test_handicap_clinched_series_team_a_wins_four_zero() -> None:
    # team_a 已 4-0 锁胜：final_a=4, final_b=0, diff=4。
    # handicap=-2.5（team_a 让 2.5 场） → cover iff diff > 2.5 → 4>2.5 True → 1
    p = series_handicap_cover_probability(_state(4, 0), Decimal("0.5"), Decimal("-2.5"))
    assert p == Decimal(1)
    # handicap=-5.5（team_a 让 5.5 场） → cover iff diff > 5.5 → 4>5.5 False → 0
    p2 = series_handicap_cover_probability(_state(4, 0), Decimal("0.5"), Decimal("-5.5"))
    assert p2 == Decimal(0)


def test_handicap_team_b_clinched_zero_four() -> None:
    # diff = -4；handicap=0 → cover iff diff > 0 → False → 0
    p = series_handicap_cover_probability(_state(0, 4), Decimal("0.5"), Decimal("0"))
    assert p == Decimal(0)


def test_single_game_cover_probability_devig_basic() -> None:
    # 给定 spread snapshot: team_a 让分 -3.5, p_a_covers=0.55 → 直接返回。
    snap = GameSpreadSnapshot(
        team_a="Boston Celtics",
        team_b="New York Knicks",
        spread_line=Decimal("-3.5"),
        p_a_covers=Decimal("0.55"),
        observed_at=_NOW,
        source="theoddsapi",
    )
    p = single_game_cover_probability(snap, team_a="Boston Celtics", handicap=Decimal("-3.5"))
    assert p == Decimal("0.55")


def test_single_game_cover_probability_team_b_perspective() -> None:
    # 让分 line=team_a -3.5；用户押 team_b +3.5 → handicap 输入 +3.5 with team_b 目标。
    snap = GameSpreadSnapshot(
        team_a="Boston Celtics",
        team_b="New York Knicks",
        spread_line=Decimal("-3.5"),
        p_a_covers=Decimal("0.55"),
        observed_at=_NOW,
        source="theoddsapi",
    )
    # team_a 视角 handicap=-3.5 时 p_a 覆盖 0.55；team_b 视角押对手让 +3.5 应得 0.45。
    p = single_game_cover_probability(snap, team_a="New York Knicks", handicap=Decimal("3.5"))
    assert p == Decimal("0.45")


def test_single_game_cover_probability_line_mismatch_returns_none() -> None:
    snap = GameSpreadSnapshot(
        team_a="Boston Celtics",
        team_b="New York Knicks",
        spread_line=Decimal("-3.5"),
        p_a_covers=Decimal("0.55"),
        observed_at=_NOW,
        source="theoddsapi",
    )
    # handicap=-5.5 与 snapshot line -3.5 完全不同 → 拒绝（None）。
    assert single_game_cover_probability(snap, team_a="Boston Celtics", handicap=Decimal("-5.5")) is None


def test_single_game_cover_probability_unknown_team_returns_none() -> None:
    snap = GameSpreadSnapshot(
        team_a="Boston Celtics",
        team_b="New York Knicks",
        spread_line=Decimal("-3.5"),
        p_a_covers=Decimal("0.55"),
        observed_at=_NOW,
        source="theoddsapi",
    )
    assert single_game_cover_probability(snap, team_a="Miami Heat", handicap=Decimal("-3.5")) is None
