"""系列赛让分 (HANDICAP) 概率模型。

支持两种 handicap scope：

- **series**: 让分定义在「整个系列赛最终比分差」上。例如「team_a -1.5 games in the
  series」表示 team_a 必须最终多赢 ≥ 2 场（即 final_a - final_b > 1.5）。
  我们枚举所有可能的最终 (final_a, final_b) 终局，用与 winner_model 同源的
  负二项闭式逐项累加。
- **single_game**: 让分定义在系列赛中某一具体场次（spreads）。pricing 由 TheOddsAPI
  spreads 端点 de-vig 得到 cover probability，直接复用。

API：
- ``series_handicap_cover_probability(state, p_per_game, handicap)``：
  返回「team_a 覆盖 handicap」的概率。沿用 sportsbook 标准：handicap 是加到
  team_a **得分**上的数（``adjusted_a = final_a + handicap``），cover ⟺
  ``adjusted_a > final_b`` ⟺ ``final_a - final_b > -handicap``。
  例如 handicap=-1.5（team_a 让分 1.5） → 必须 final_a - final_b > 1.5
       （多赢 ≥ 2 场，更严苛）；
       handicap=+1.5（team_a 受让 1.5） → 必须 final_a - final_b > -1.5
       （可以输 1 场以内也算赢）。
- ``single_game_cover_probability(spread_snapshot, handicap)``：
  从 spread snapshot 中拿 team_a 在 handicap 上的 de-vig 后概率。
"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.workflow._shared.team_normalize import normalize_team_name
from polymarket_trader.workflow.series.match import GameSpreads
from polymarket_trader.workflow.series.types import SeriesState


def series_handicap_cover_probability(
    state: SeriesState,
    p_per_game: Decimal,
    handicap: Decimal,
) -> Decimal:
    """team_a 覆盖系列赛 handicap 的概率，handicap 半整数避免 push。

    Sportsbook 约定：handicap 加到 team_a 得分上 → cover ⟺
    ``(final_a - final_b) > -handicap``。

    退化：系列赛已分胜负时按既定终局判定（needed_a<=0 → final_a 锁定为
    needed_wins，final_b 锁定为 wins_b；needed_b<=0 同理）。
    """

    threshold = -handicap
    needed_total = _needed_wins(state.best_of)
    needed_a = needed_total - state.wins_a
    needed_b = needed_total - state.wins_b
    if needed_a <= 0:
        # team_a 已锁胜：final_a = needed_total, final_b = state.wins_b
        diff = Decimal(needed_total - state.wins_b)
        return Decimal(1) if diff > threshold else Decimal(0)
    if needed_b <= 0:
        # team_b 已锁胜
        diff = Decimal(state.wins_a - needed_total)
        return Decimal(1) if diff > threshold else Decimal(0)
    p = _clamp_unit(p_per_game)
    q = Decimal(1) - p
    p_pow_needed_a = _pow_nonneg(p, needed_a)
    q_pow_needed_b = _pow_nonneg(q, needed_b)
    total = Decimal(0)
    # team_a 拿下系列赛（final_a = needed_total），team_b 赢 j 场 → final_b = wins_b + j。
    for j in range(needed_b):
        final_a = needed_total
        final_b = state.wins_b + j
        diff = Decimal(final_a - final_b)
        if diff > threshold:
            prob = Decimal(_binomial(needed_a + j - 1, j)) * p_pow_needed_a * _pow_nonneg(q, j)
            total += prob
    # team_b 拿下系列赛（final_b = needed_total），team_a 赢 i 场。
    for i in range(needed_a):
        final_a = state.wins_a + i
        final_b = needed_total
        diff = Decimal(final_a - final_b)
        if diff > threshold:
            prob = Decimal(_binomial(needed_b + i - 1, i)) * q_pow_needed_b * _pow_nonneg(p, i)
            total += prob
    return _clamp_unit(total)


def single_game_cover_probability(
    spread_snapshot: GameSpreads,
    *,
    team_a: str,
    handicap: Decimal,
) -> Decimal | None:
    """从 spread snapshot 取 ``team_a`` 在 ``handicap`` 上的覆盖概率。

    匹配规则（穷举 2×2 = 4 个组合，每个都对应同一个底层 bet 的某种表达）：

    - target == snap.team_a 且 handicap == spread_line → 直接返回 ``p_a_covers``。
    - target == snap.team_b 且 handicap == -spread_line → 返回 ``1 - p_a_covers``
      （即 team_b 在镜像 line 上的覆盖概率）。
    - 其他组合（数据不一致或不同 line）→ None，evaluator 报 MISSING_GAME_SPREADS。
    """

    target_norm = normalize_team_name(team_a)
    snap_a_norm = normalize_team_name(spread_snapshot.team_a)
    snap_b_norm = normalize_team_name(spread_snapshot.team_b)
    if not target_norm:
        return None
    p_a = spread_snapshot.p_a_covers
    if _same_team(target_norm, snap_a_norm) and spread_snapshot.spread_line == handicap:
        return p_a
    if _same_team(target_norm, snap_b_norm) and spread_snapshot.spread_line == -handicap:
        return Decimal(1) - p_a
    return None


def _same_team(target_norm: str, snap_norm: str) -> bool:
    """全名相等或 last-token 相等（"Boston Celtics" ↔ "Celtics"）。"""

    if not target_norm or not snap_norm:
        return False
    if target_norm == snap_norm:
        return True
    return target_norm.split()[-1] == snap_norm.split()[-1]


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
    "series_handicap_cover_probability",
    "single_game_cover_probability",
]
