"""系列赛胜者概率模型：负二项分布闭式解。

给定系列赛热态（已胜场 / best_of）+ 单场胜率，求 team_a 赢下整个 best-of-N
系列赛的概率。系列赛在不允许平局假设下相互独立——同一对手单场胜率 p 不变。

公式：
    needed_a = ceil(best_of / 2) - wins_a   # team_a 还需要的胜场
    needed_b = ceil(best_of / 2) - wins_b   # team_b 还需要的胜场
    P(team_a wins series) =
        Σ_{i=0..needed_b-1} C(needed_a + i - 1, i) * p^needed_a * (1-p)^i

i 枚举 "team_a 拿下 needed_a 场前，team_b 输了多少场"。
退化：
- needed_a <= 0  → team_a 已锁胜，概率 = 1
- needed_b <= 0  → team_b 已锁胜（或对手已先赢够），team_a 概率 = 0

全部用 Decimal；幂运算用整数 needed_a × log 求解会失精度，所以直接按定义乘。
"""

from __future__ import annotations

from decimal import Decimal
from math import comb

from polymarket_trader.quant.series.types import SeriesState


def series_win_probability(state: SeriesState, p_per_game: Decimal) -> Decimal:
    """team_a 在给定 series state 与单场胜率 p_per_game 下赢下系列赛的概率。

    参数：
        state: 系列赛热态。``wins_a`` / ``wins_b`` 与 ``best_of`` 决定剩余需求。
        p_per_game: team_a 在单场比赛中获胜的概率。Decimal in [0, 1]。

    返回：[0, 1] 区间的 Decimal。
    """

    needed_a = _needed_wins(state.best_of) - state.wins_a
    needed_b = _needed_wins(state.best_of) - state.wins_b
    if needed_a <= 0:
        return Decimal(1)
    if needed_b <= 0:
        return Decimal(0)
    p = _clamp_unit(p_per_game)
    q = Decimal(1) - p
    # 负二项分布求和：team_a 先赢 needed_a 场，对方 i 场（i ∈ [0, needed_b-1]）。
    # 由于 needed_a / needed_b 通常 ≤ 4（best-of-7），直接展开比较直观。
    probability = Decimal(0)
    p_pow_needed_a = _pow_nonneg(p, needed_a)
    for i in range(needed_b):
        # 组合数 C(needed_a + i - 1, i) —— 出现位置数。
        comb = _binomial(needed_a + i - 1, i)
        probability += Decimal(comb) * p_pow_needed_a * _pow_nonneg(q, i)
    # 数值上限避免轻微浮点累积误差越过 1。
    if probability > Decimal(1):
        return Decimal(1)
    if probability < Decimal(0):
        return Decimal(0)
    return probability


def expected_games_remaining(state: SeriesState, p_per_game: Decimal) -> float:
    """期望系列赛剩余场数（负二项分布加权均值）。

    给定当前比分 (wins_a, wins_b) 与单场胜率，算出系列赛预计还需要打几场。
    best-of-7 且 3-0 领先时期望约 1.6 场；0-0 均势时期望约 5.8 场。
    """
    needed_a = _needed_wins(state.best_of) - state.wins_a
    needed_b = _needed_wins(state.best_of) - state.wins_b
    if needed_a <= 0 or needed_b <= 0:
        return 0.0
    p = max(0.0, min(1.0, float(p_per_game)))
    q = 1.0 - p
    expected = 0.0
    for g in range(min(needed_a, needed_b), needed_a + needed_b):
        p_a = 0.0
        if g >= needed_a and (g - needed_a) < needed_b:
            p_a = comb(g - 1, needed_a - 1) * (p ** needed_a) * (q ** (g - needed_a))
        p_b = 0.0
        if g >= needed_b and (g - needed_b) < needed_a:
            p_b = comb(g - 1, needed_b - 1) * (q ** needed_b) * (p ** (g - needed_b))
        expected += g * (p_a + p_b)
    return max(expected, 0.0)


def team_b_win_probability(state: SeriesState, p_per_game: Decimal) -> Decimal:
    """team_b 在给定 series state 与 team_a 单场胜率下赢下系列赛的概率。

    best-of-N 不允许平局：team_a 与 team_b 必有一胜，互补成立。
    """

    return Decimal(1) - series_win_probability(state, p_per_game)


def _needed_wins(best_of: int) -> int:
    """先胜 ceil(best_of / 2) 场即取系列赛。best_of=7 → 4；best_of=5 → 3。"""

    if best_of <= 0:
        raise ValueError(f"best_of must be positive, got {best_of}")
    return (best_of + 1) // 2


def _clamp_unit(value: Decimal) -> Decimal:
    if value < Decimal(0):
        return Decimal(0)
    if value > Decimal(1):
        return Decimal(1)
    return value


def _pow_nonneg(base: Decimal, exponent: int) -> Decimal:
    """Decimal 的 0**0 抛 InvalidOperation——这里按数学约定取 1，避免边界崩溃。"""

    if exponent < 0:
        raise ValueError(f"exponent must be non-negative, got {exponent}")
    if exponent == 0:
        return Decimal(1)
    return base**exponent


def _binomial(n: int, k: int) -> int:
    """C(n, k)，n>=0 且 0<=k<=n；其他视为 0。"""

    if n < 0 or k < 0 or k > n:
        return 0
    if k > n - k:
        k = n - k
    result = 1
    for i in range(k):
        result = result * (n - i) // (i + 1)
    return result


__all__ = ["expected_games_remaining", "series_win_probability", "team_b_win_probability"]
