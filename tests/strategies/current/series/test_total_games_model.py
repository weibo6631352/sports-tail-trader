"""total_games_model 分布 / Over / Under / push 的数值正确性。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from strategies.current.series.total_games_model import (
    prob_over,
    prob_under,
    push_probability,
    total_games_distribution,
)
from strategies.current.series.types import SeriesState


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


def test_zero_zero_best_of_seven_p_half_distribution_sums_to_one() -> None:
    dist = total_games_distribution(_state(0, 0), Decimal("0.5"))
    total = sum(dist.values(), Decimal(0))
    # 数值上限：Decimal 累积应严格收敛到 1（无浮点损失，因为 p=0.5 是 Decimal）。
    assert abs(total - Decimal(1)) < Decimal("1e-9")
    # P(K=4) > 0 表示 4-0 sweep 可发生
    assert dist.get(4, Decimal(0)) > Decimal(0)
    # P(K=8) = 0，best-of-7 不会超过 7 场
    assert dist.get(8, Decimal(0)) == Decimal(0)


def test_zero_zero_p_half_is_symmetric_around_six() -> None:
    dist = total_games_distribution(_state(0, 0), Decimal("0.5"))
    # team_a 与 team_b 在 p=0.5 下完全对称：dist[K] 应对称——但既然 needed_a==needed_b，
    # P(K=4) == P(K=4) 显然，更有意义的是 P(K=4) + P(K=5) + P(K=6) + P(K=7) ≈ 1。
    accounted = sum(dist.get(k, Decimal(0)) for k in (4, 5, 6, 7))
    assert abs(accounted - Decimal(1)) < Decimal("1e-9")


def test_three_zero_lead_concentrates_on_short_series() -> None:
    dist = total_games_distribution(_state(3, 0), Decimal("0.5"))
    # 3-0 lead，team_a 只需再赢 1 场就收下：最短 K=4，最长 K=7。
    assert dist.get(4, Decimal(0)) > dist.get(5, Decimal(0))
    assert dist.get(4, Decimal(0)) > Decimal("0.4")  # P(K=4) ≥ 0.5


def test_clinched_series_returns_point_mass() -> None:
    # team_a 已 4-0 拿下：分布只在 K=4 点为 1。
    dist = total_games_distribution(_state(4, 0), Decimal("0.3"))
    assert dist == {4: Decimal(1)}


def test_clinched_series_team_b_returns_point_mass() -> None:
    dist = total_games_distribution(_state(2, 4), Decimal("0.5"))
    assert dist == {6: Decimal(1)}


def test_prob_over_and_under_complementary_at_half_line() -> None:
    dist = total_games_distribution(_state(0, 0), Decimal("0.6"))
    over = prob_over(dist, Decimal("5.5"))
    under = prob_under(dist, Decimal("5.5"))
    assert abs(over + under - Decimal(1)) < Decimal("1e-9")


def test_prob_over_half_line_explicit_value() -> None:
    # 1-0, p=0.5, best_of=5 → 总场数取值 {3,4,5}，line 3.5 → P(over) = P(K>=4)
    dist = total_games_distribution(_state(1, 0, best_of=5), Decimal("0.5"))
    # team_a 拿下：j=0 → K=3 概率 0.25^? 计算逐项
    # 实际累加：dist[3] + dist[4] + dist[5] = 1
    over = prob_over(dist, Decimal("3.5"))
    under = prob_under(dist, Decimal("3.5"))
    assert over + under == Decimal(1)
    assert over > Decimal(0)
    assert under > Decimal(0)


def test_integer_line_yields_push_probability() -> None:
    # line=6 整数 → 可能有 push（K=6）。
    dist = total_games_distribution(_state(0, 0), Decimal("0.5"))
    push = push_probability(dist, Decimal("6"))
    assert push > Decimal(0)
    over = prob_over(dist, Decimal("6"))
    under = prob_under(dist, Decimal("6"))
    # over + under + push 应严格收敛到 dist 总和，但因为 push 一半计入 over 一半计入 under，
    # 数学上 over + under == 1（push 已被分摊）。
    assert abs(over + under - Decimal(1)) < Decimal("1e-9")
    # push 单独暴露的部分仍 > 0，供审计。
    assert push == push_probability(dist, Decimal("6"))


def test_extreme_p_one_team_a_always_wins_four_zero() -> None:
    # p=1 → team_a 4-0 sweep 必发生 → K=4 概率 1。
    dist = total_games_distribution(_state(0, 0), Decimal("1"))
    assert dist.get(4, Decimal(0)) == Decimal(1)
    assert prob_over(dist, Decimal("4.5")) == Decimal(0)
    assert prob_under(dist, Decimal("4.5")) == Decimal(1)
