"""体育盘口数学锁定评估(归一化 lock_probability + 详细审计)。

每类盘口的"剩余比赛能否让我方持仓输/赢"概率算法不同(MLB Totals 用单半局得分
泊松分布、Soccer HT 用伤停时间、NBA ML 用剩余分钟+分差等),本模块用统一
``MathLockResult`` 接口让上层(exit_overlay / odds_gap / entry gates)拿到归一化
[0,1] 锁定度 + 关键变量,无需各自实现锁定算法。

设计原则:
- ``lock_probability=1.0`` = 数学上 100% 锁定(剩余赛果无法影响持仓胜负)
- ``lock_probability=0.0`` = 完全不锁定 / 不支持的盘口类型
- 中间值反映真实剩余胜率(基于历史 base rate);策略可按阈值取舍
- ``details`` 暴露关键变量(剩余时间、比分差、line gap 等)供审计/调参
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from strategies.sports_framework.types import (
    LiveGameState,
    LiveGameStatus,
    SportsMarketSide,
    SportsMarketType,
)


@dataclass(frozen=True, slots=True)
class MathLockResult:
    """数学锁定评估结果,归一化 + 可审计。"""

    lock_probability: Decimal  # [0, 1],1=full lock
    method: str   # 算法名: mlb_totals_under / soccer_ht_clock / ...
    reason: str   # "fully_locked" / "near_lock" / "not_supported" / "still_volatile"
    details: Mapping[str, Any] = field(default_factory=dict)


_ZERO = Decimal("0")
_ONE = Decimal("1")
_UNSUPPORTED = MathLockResult(
    lock_probability=_ZERO,
    method="unsupported",
    reason="market_or_sport_not_supported",
    details={},
)


# ============================================================
# MLB Totals (Over/Under, full game)
# ============================================================

# MLB 单半局得分历史 base rate(近似 Poisson λ≈0.55 → P(N≥k)):
# 1 分: ~30%, 2 分: ~10%, 3 分: ~4%, 4 分: ~1.5%, 5 分: ~0.5%
# 这些是单半局事件;剩余多个半局时复合概率累加(独立假设)。
_MLB_HALF_INNING_SCORE_DIST: dict[int, Decimal] = {
    0: Decimal("0.70"),
    1: Decimal("0.20"),
    2: Decimal("0.06"),
    3: Decimal("0.025"),
    4: Decimal("0.010"),
    5: Decimal("0.004"),
    6: Decimal("0.001"),
}


def _mlb_remaining_half_innings(state: Any) -> int:
    """剩余 half-innings: B9th 之后剩 1(只 B9 1 个半局,客胜场无 B9 = 0),否则按当前 inning 推。"""

    if state is None or state.current_inning is None:
        return 0
    inning = int(state.current_inning)
    half = (state.inning_half or "top").lower()
    if inning >= 10:
        return 1 if half == "top" else 0  # extra innings 通常一打就完
    if inning >= 9:
        return 1 if half == "top" else 0  # B9 主队不打或最后一击
    # 第 8 局及以前: top 之后 = 1 (B同局) + 后续完整局; bottom 之后 = 后续完整局
    completed_half_innings = (inning - 1) * 2 + (1 if half == "bottom" else 0)
    return max(0, 18 - completed_half_innings - 1)


def _mlb_totals_lock(
    side: SportsMarketSide,
    line: Decimal,
    game: LiveGameState,
) -> MathLockResult:
    """MLB Totals U/O 数学锁定: 剩余 half-innings 单独得分独立 Poisson 近似。"""

    state = game.baseball_state
    if state is None or state.current_inning is None:
        return MathLockResult(_ZERO, "mlb_totals", "missing_baseball_state", {})
    home = int(game.home_score or 0)
    away = int(game.away_score or 0)
    total = Decimal(int(home) + int(away))

    if side == SportsMarketSide.UNDER:
        if total >= line:
            # 总分已达 line,Under 已输:lock=1 但是反向(我们输)。
            return MathLockResult(
                _ONE,
                "mlb_totals_under",
                "already_exceeded_line_lose",
                {"total": str(total), "line": str(line), "outcome": "lose"},
            )
        remaining_window = line - total
        n_half = _mlb_remaining_half_innings(state)
        if n_half <= 0:
            return MathLockResult(
                _ONE,
                "mlb_totals_under",
                "no_remaining_half_innings",
                {"total": str(total), "line": str(line), "outcome": "win"},
            )
        # P(总剩余得分 ≥ remaining_window)的简化估计:每半局得 ≥ ceil(remaining_window/n_half) 的概率。
        # 近似:总得 ≥ k = n_half × λ ≈ Poisson(0.55 × n_half) ≥ k
        # 这里用更保守的"任一半局得 ≥ remaining_window 分"近似下限。
        # 实战:剩余 window 大 + 半局少 → 锁定概率高。
        per_half_need = int(remaining_window) + 1  # ceiling
        if per_half_need > 6:
            p_break = Decimal("0.001")  # 极小概率
        else:
            p_break = _MLB_HALF_INNING_SCORE_DIST.get(per_half_need, Decimal("0.001"))
        # 多半局复合: P(任一半局 break) ≈ 1 - (1-p)^n
        p_no_break = (_ONE - p_break) ** n_half
        lock_prob = (_ONE - (_ONE - p_no_break)).quantize(Decimal("0.0001"))
        # ↑ 这是 P(无半局打破) 即 Under 赢概率
        return MathLockResult(
            lock_prob,
            "mlb_totals_under",
            "live_estimate",
            {
                "total": str(total),
                "line": str(line),
                "remaining_window": str(remaining_window),
                "remaining_half_innings": n_half,
                "per_half_need": per_half_need,
                "p_break_per_half": str(p_break),
            },
        )
    if side == SportsMarketSide.OVER:
        if total > line:
            return MathLockResult(
                _ONE,
                "mlb_totals_over",
                "already_exceeded_line_win",
                {"total": str(total), "line": str(line), "outcome": "win"},
            )
        remaining_window = line - total + Decimal("0.5")  # 还需要这么多得分突破
        n_half = _mlb_remaining_half_innings(state)
        if n_half <= 0:
            return MathLockResult(
                _ONE,
                "mlb_totals_over",
                "no_remaining_half_innings",
                {"total": str(total), "line": str(line), "outcome": "lose"},
            )
        per_half_need = int(remaining_window) + 1
        if per_half_need > 6:
            p_break = Decimal("0.001")
        else:
            p_break = _MLB_HALF_INNING_SCORE_DIST.get(per_half_need, Decimal("0.001"))
        p_no_break = (_ONE - p_break) ** n_half
        # Over 锁定 = P(break)
        lock_prob = (_ONE - p_no_break).quantize(Decimal("0.0001"))
        return MathLockResult(
            lock_prob,
            "mlb_totals_over",
            "live_estimate",
            {
                "total": str(total),
                "line": str(line),
                "remaining_window": str(remaining_window),
                "remaining_half_innings": n_half,
                "per_half_need": per_half_need,
                "p_break_per_half": str(p_break),
            },
        )
    return _UNSUPPORTED


# ============================================================
# Soccer Halftime Result (home / draw / away)
# ============================================================

# 足球单分钟进球 base rate:~0.025 (1 球 / 40min average) → λ ≈ 0.025/min
# 1st half 末段(40+min)单球期望低,扳平/反超概率随时间收缩
_SOCCER_GOAL_RATE_PER_MIN = Decimal("0.025")


def _soccer_halftime_lock(
    side: SportsMarketSide,  # YES / NO
    direction: str,  # "home" / "draw" / "away"
    game: LiveGameState,
) -> MathLockResult:
    """Soccer Halftime Result 锁定: 基于剩余 1st half 时间 + 当前比分计算扳平/反超概率。"""

    state = game.soccer_state
    if state is None:
        return MathLockResult(_ZERO, "soccer_halftime", "missing_soccer_state", {})
    period = (state.period or "").lower()
    home = int(game.home_score or 0)
    away = int(game.away_score or 0)
    home_lead = int(home) - int(away)
    actual = "home" if home_lead > 0 else ("away" if home_lead < 0 else "draw")

    # 半场已结束 / 比赛已结束 → 数学完全锁定
    if period in {"second_half", "ended", "full_time", "halftime"}:
        won = actual == direction
        if side == SportsMarketSide.YES:
            return MathLockResult(
                _ONE if won else _ZERO,
                "soccer_halftime",
                "halftime_settled",
                {"actual": actual, "direction": direction, "score": f"{home}-{away}"},
            )
        # NO side
        return MathLockResult(
            _ZERO if won else _ONE,
            "soccer_halftime",
            "halftime_settled",
            {"actual": actual, "direction": direction, "score": f"{home}-{away}"},
        )

    if period != "first_half":
        return MathLockResult(_ZERO, "soccer_halftime", "unknown_period", {"period": period})

    clock = int(state.clock_minutes or 0)
    remaining_min = max(0, 45 - clock) + 3  # 包括 ~3 分钟伤停时间
    # P(在剩余时间内进 1+ 球) ≈ 1 - (1-λ)^t,t 单位分钟,双方各算 λ。
    # 简化(每分钟单边进球 ~2.5%)
    # 这是任一进球的概率;扳平/反超需要进球
    p_any_goal = _ONE - (_ONE - _SOCCER_GOAL_RATE_PER_MIN) ** (Decimal(remaining_min) * Decimal("2"))
    # 进球后改变 actual 方向的条件概率: 平局变赢/输各 50%; 领先方进球加强领先(不变) 50% (assume)
    # 简化:进球 → 50% 改方向
    p_change = p_any_goal * Decimal("0.5")
    # actual 不变概率 = 1 - p_change
    p_stay = _ONE - p_change

    won = actual == direction
    if side == SportsMarketSide.YES:
        # YES 锁定 = won AND actual 不变
        lock_prob = (p_stay if won else (_ONE - p_stay) * Decimal("0.5")).quantize(Decimal("0.0001"))
    else:
        # NO 锁定 = !won AND actual 不变 (won 方向不发生)
        lock_prob = ((_ONE - p_stay) * Decimal("0.5") if won else p_stay).quantize(Decimal("0.0001"))

    return MathLockResult(
        lock_prob,
        "soccer_halftime",
        "live_estimate",
        {
            "period": period,
            "clock_minutes": clock,
            "remaining_min": remaining_min,
            "score": f"{home}-{away}",
            "actual": actual,
            "direction": direction,
            "p_change": str(p_change),
        },
    )


# ============================================================
# 统一分派
# ============================================================

def evaluate_math_lock(
    market_type: SportsMarketType,
    side: SportsMarketSide,
    line: Decimal | None,
    game: LiveGameState | None,
    *,
    market_slug: str | None = None,
) -> MathLockResult:
    """统一数学锁定评估。按 (market_type, side, game.sport) 分派。

    不支持的盘口返回 ``lock_probability=0``,strategy 可选择 fallback 到其他信号。
    """

    if game is None:
        return MathLockResult(_ZERO, "no_game", "missing_live_game_state", {})
    if game.status == LiveGameStatus.ENDED:
        # 已结束:lock 由 score + line 直接判定(留给上层用)
        return MathLockResult(
            _ONE,
            "game_ended",
            "game_already_ended",
            {"home_score": game.home_score, "away_score": game.away_score},
        )

    if market_type == SportsMarketType.TOTALS and line is not None and game.baseball_state is not None:
        return _mlb_totals_lock(side, line, game)

    if market_type == SportsMarketType.BINARY_PROP and game.soccer_state is not None:
        slug = (market_slug or "").lower()
        for direction in ("home", "draw", "away"):
            if slug.endswith(f"halftime-result-{direction}"):
                return _soccer_halftime_lock(side, direction, game)

    return _UNSUPPORTED


__all__ = [
    "MathLockResult",
    "evaluate_math_lock",
]
