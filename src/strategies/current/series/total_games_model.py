"""系列赛总场数 (TOTAL GAMES O/U) 分布与 Over/Under 概率。

给定系列赛热态（已胜场 / best_of）+ 单场 team_a 胜率 p，求系列赛**最终总场数** K
的分布（K = wins_a_final + wins_b_final）。Over/Under 概率从分布上累加得到。

数学公式（与 winner_model 同源）：
- needed_a = ceil(best_of / 2) - wins_a，needed_b 同理
- team_a 先收下系列赛时，team_b 还赢 j 场（j ∈ [0, needed_b-1]）：
    final_a = needed_wins(best_of)
    final_b = wins_b + j
    K = wins_a + wins_b + (needed_a + j)
    Pr = C(needed_a + j - 1, j) * p^needed_a * q^j
- 对称地 team_b 先收时：Pr = C(needed_b + i - 1, i) * q^needed_b * p^i

退化：一方已锁胜（needed <= 0）→ K = wins_a + wins_b 的 point mass = 1，
其余 K 为 0。

Over/Under 处理：
- 半整数 line（5.5）：直接按整数 K 比较，无 push。
- 整数 line（6）：恰好 K=line 视为 push；本模块输出 P(over), P(under),
  P(push) 三个独立量，**默认 prob_over / prob_under 平分 push 一半**，
  push_probability 单独暴露在 metadata 里以便上游审计。Polymarket 现行多数
  total_games 市场使用半整数 line，但保留整数支持以覆盖少数变体盘口。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from strategies.current.series.types import SeriesState


_HALF = Decimal("1") / Decimal("2")


def total_games_distribution(
    state: SeriesState,
    p_per_game: Decimal,
) -> Mapping[int, Decimal]:
    """返回系列赛最终总场数 K 的离散概率分布。

    输出：``{K: P(K)}``，K 取值范围 ``[wins_a + wins_b + min_remaining,
    wins_a + wins_b + max_remaining]``；分布 Σ=1（数值上限 Decimal 累加）。

    参数：
        state: 系列赛热态。
        p_per_game: team_a 单场胜率，Decimal in [0, 1]。
    """

    needed_total = _needed_wins(state.best_of)
    needed_a = needed_total - state.wins_a
    needed_b = needed_total - state.wins_b
    base_games = state.wins_a + state.wins_b
    # 退化：一方已锁胜（含越界），系列赛在 base_games 当前总场数处终结。
    if needed_a <= 0 or needed_b <= 0:
        return {base_games: Decimal(1)}
    p = _clamp_unit(p_per_game)
    q = Decimal(1) - p
    distribution: dict[int, Decimal] = {}
    p_pow_needed_a = _pow_nonneg(p, needed_a)
    q_pow_needed_b = _pow_nonneg(q, needed_b)
    # team_a 收下系列赛时，对手 b 输了 needed_a 场、赢 j 场。
    for j in range(needed_b):
        prob = Decimal(_binomial(needed_a + j - 1, j)) * p_pow_needed_a * _pow_nonneg(q, j)
        k = base_games + needed_a + j
        distribution[k] = distribution.get(k, Decimal(0)) + prob
    # team_b 收下系列赛时对称分布。
    for i in range(needed_a):
        prob = Decimal(_binomial(needed_b + i - 1, i)) * q_pow_needed_b * _pow_nonneg(p, i)
        k = base_games + needed_b + i
        distribution[k] = distribution.get(k, Decimal(0)) + prob
    return distribution


def prob_over(distribution: Mapping[int, Decimal], line: Decimal) -> Decimal:
    """P(K > line)。整数 line 把 P(K == line) 视为 push 并取一半（默认约定）。"""

    over, _under, _push = _split(distribution, line)
    return over


def prob_under(distribution: Mapping[int, Decimal], line: Decimal) -> Decimal:
    """P(K < line)。整数 line 把 P(K == line) 视为 push 并取一半（默认约定）。"""

    _over, under, _push = _split(distribution, line)
    return under


def push_probability(distribution: Mapping[int, Decimal], line: Decimal) -> Decimal:
    """整数 line 上的 push 概率（K 恰好等于 line）；半整数 line → 0。"""

    _over, _under, push = _split(distribution, line)
    return push


def _split(
    distribution: Mapping[int, Decimal],
    line: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """返回 ``(over, under, push)``。整数 line 上把 push 一半计入 over、一半计入 under。

    约定：Polymarket 半整数 line 没有 push；整数 line 历史上极少见但若出现，
    push 一半归 over 一半归 under 是 fair 假设。push_probability 单独暴露便于审计。
    """

    over_strict = Decimal(0)
    under_strict = Decimal(0)
    push = Decimal(0)
    for k, prob in distribution.items():
        k_dec = Decimal(k)
        if k_dec > line:
            over_strict += prob
        elif k_dec < line:
            under_strict += prob
        else:
            push += prob
    over = over_strict + push * _HALF
    under = under_strict + push * _HALF
    return _clamp_unit(over), _clamp_unit(under), _clamp_unit(push)


def _needed_wins(best_of: int) -> int:
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
    """Decimal 的 0**0 抛 InvalidOperation——按数学约定取 1，避免边界崩溃。"""

    if exponent < 0:
        raise ValueError(f"exponent must be non-negative, got {exponent}")
    if exponent == 0:
        return Decimal(1)
    return base**exponent


def _binomial(n: int, k: int) -> int:
    if n < 0 or k < 0 or k > n:
        return 0
    if k > n - k:
        k = n - k
    result = 1
    for i in range(k):
        result = result * (n - i) // (i + 1)
    return result


__all__ = [
    "prob_over",
    "prob_under",
    "push_probability",
    "total_games_distribution",
]
