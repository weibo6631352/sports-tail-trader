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

import math
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
# Baseball Moneyline (MLB / KBO / NPB,任何 baseball league)
# ============================================================

# 单半局得分 variance ≈ 1.0(均值 0.55,泊松近似)。reversal 需要 opponent 净
# 得分超过 lead,用正态近似 P(reversal) = exp(-lead² / (2 × var_total))。
_BASEBALL_HALF_INNING_VAR = 1.0


def _baseball_moneyline_lock(
    side: SportsMarketSide,
    game: LiveGameState,
) -> MathLockResult:
    """Baseball Moneyline 数学锁定: 基于 lead × 剩余半局 normal-approx reversal 概率。"""

    state = game.baseball_state
    if state is None or state.current_inning is None:
        return MathLockResult(_ZERO, "baseball_ml", "missing_baseball_state", {})
    home = int(game.home_score or 0)
    away = int(game.away_score or 0)

    if side == SportsMarketSide.HOME:
        lead = home - away
        side_name = "home"
    elif side == SportsMarketSide.AWAY:
        lead = away - home
        side_name = "away"
    else:
        return MathLockResult(_ZERO, "baseball_ml", "unsupported_side", {})

    if lead <= 0:
        # 我方落后或平局,不锁定(实际胜率 < 50%,不用 math_lock 作进场信号)
        return MathLockResult(
            _ZERO,
            "baseball_ml",
            "side_not_leading",
            {"lead": lead, "home": home, "away": away, "side": side_name},
        )

    n_half = _mlb_remaining_half_innings(state)
    if n_half <= 0:
        # 已无剩余半局 → 锁定
        return MathLockResult(
            _ONE,
            "baseball_ml",
            "no_remaining_half_innings",
            {"lead": lead, "side": side_name, "outcome": "win"},
        )

    # 差异随机变量 X = (对手剩余得分 - 我方剩余得分): mean=0, var = 2 × var_per_half × n_half
    # 双方各打约 n_half/2 半局,合并 variance:
    var_diff = 2.0 * _BASEBALL_HALF_INNING_VAR * (n_half / 2.0)
    # P(reversal) = P(X >= lead + 1) ≈ Φ(-lead/sqrt(var_diff))(1-tail 正态 CDF)
    # 用 erfc 表达: Φ(-z) = 0.5 × erfc(z/sqrt(2))
    if var_diff <= 0.0001:
        p_reversal = 0.0
    else:
        z = lead / math.sqrt(var_diff)
        p_reversal = 0.5 * math.erfc(z / math.sqrt(2.0))
    lock_p = max(0.0, min(1.0, 1.0 - p_reversal))
    lock_dec = Decimal(str(round(lock_p, 4)))

    return MathLockResult(
        lock_dec,
        "baseball_ml",
        "live_estimate",
        {
            "lead": lead,
            "home": home,
            "away": away,
            "side": side_name,
            "remaining_half_innings": n_half,
            "var_diff": var_diff,
            "p_reversal": round(p_reversal, 4),
        },
    )


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
# Sport-specific lock 公式
# 每种 sport × market_type 单独建模——不要 generic 兜底，没建模的盘口
# 返回 unsupported 让上层 fallback 到 goalserve_implied / microprice。
# ============================================================


# ---------- Basketball (NBA / WNBA / CBA) ----------
# 单边每秒得分均值/方差（从 NBA 赛季均值反推）：
#   ~110 pts/team/48min ≈ 0.038 pts/sec/team
#   单次得分大小分布：2 pts (~55%), 3 pts (~30%), 1 pt FT (~15%)
#   单秒得分方差 ≈ E[X²]×rate - E[X]²×rate² ≈ 0.18 per sec
_BASKETBALL_VAR_PER_SECOND = 0.18


def _basketball_ml_lock(side: SportsMarketSide, game: LiveGameState) -> MathLockResult:
    """NBA/WNBA ML：lead × 剩余秒数双边正态近似 reversal。"""
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return MathLockResult(_ZERO, "basketball_ml", "unsupported_side", {})
    lead = game.score_diff_for(side)
    if lead <= 0:
        return MathLockResult(_ZERO, "basketball_ml", "side_not_leading", {"lead": lead})
    remaining = game.seconds_remaining
    if remaining is None or remaining <= 0:
        return MathLockResult(_ONE, "basketball_ml", "no_remaining_time", {"lead": lead})
    var_diff = 2.0 * _BASKETBALL_VAR_PER_SECOND * remaining
    z = lead / math.sqrt(var_diff)
    p_reversal = 0.5 * math.erfc(z / math.sqrt(2.0))
    lock = max(0.0, min(1.0, 1.0 - p_reversal))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "basketball_ml", "live_estimate",
        {"lead": lead, "remaining_seconds": remaining, "p_reversal": round(p_reversal, 4)},
    )


def _basketball_totals_lock(
    side: SportsMarketSide, line: Decimal, game: LiveGameState
) -> MathLockResult:
    """NBA Totals：双边总得分 Poisson-normal 近似。"""
    if side not in {SportsMarketSide.OVER, SportsMarketSide.UNDER}:
        return MathLockResult(_ZERO, "basketball_totals", "unsupported_side", {})
    total = Decimal(int(game.total_score))
    remaining = game.seconds_remaining
    if remaining is None or remaining <= 0:
        won = (total > line) if side == SportsMarketSide.OVER else (total < line)
        return MathLockResult(
            _ONE if won else _ZERO, "basketball_totals", "game_ended",
            {"total": str(total), "line": str(line)},
        )
    if side == SportsMarketSide.OVER and total > line:
        return MathLockResult(_ONE, "basketball_totals", "already_over_win", {})
    if side == SportsMarketSide.UNDER and total >= line:
        return MathLockResult(_ZERO, "basketball_totals", "already_over_lose", {})
    var_total = 2.0 * _BASKETBALL_VAR_PER_SECOND * remaining
    need = float(line) - float(total) + (0.5 if side == SportsMarketSide.OVER else 0.0)
    z = need / math.sqrt(var_total)
    p_break = 0.5 * math.erfc(z / math.sqrt(2.0))
    lock = p_break if side == SportsMarketSide.OVER else (1.0 - p_break)
    lock = max(0.0, min(1.0, lock))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "basketball_totals", "live_estimate",
        {"total": str(total), "line": str(line), "remaining_seconds": remaining,
         "p_break": round(p_break, 4)},
    )


def _basketball_spreads_lock(
    side: SportsMarketSide, line: Decimal, game: LiveGameState
) -> MathLockResult:
    """NBA Spreads：让分后净 lead 正态近似 reversal。"""
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return MathLockResult(_ZERO, "basketball_spreads", "unsupported_side", {})
    raw_lead = game.score_diff_for(side)
    adjusted = float(raw_lead) + (float(line) if side == SportsMarketSide.HOME else -float(line))
    remaining = game.seconds_remaining
    if remaining is None or remaining <= 0:
        return MathLockResult(
            _ONE if adjusted > 0 else _ZERO, "basketball_spreads", "no_remaining_time",
            {"adjusted_lead": adjusted},
        )
    if adjusted <= 0:
        return MathLockResult(_ZERO, "basketball_spreads", "not_leading_after_handicap",
                              {"adjusted_lead": adjusted})
    var_diff = 2.0 * _BASKETBALL_VAR_PER_SECOND * remaining
    z = adjusted / math.sqrt(var_diff)
    p_reversal = 0.5 * math.erfc(z / math.sqrt(2.0))
    lock = max(0.0, min(1.0, 1.0 - p_reversal))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "basketball_spreads", "live_estimate",
        {"raw_lead": raw_lead, "line": str(line), "adjusted_lead": adjusted,
         "remaining_seconds": remaining, "p_reversal": round(p_reversal, 4)},
    )


# ---------- Soccer (EPL / J1 / J2 / 各联赛) ----------
# 单边进球率：平均比赛 ~2.6 goals/match / 90min ≈ 0.0144 goals/min/team
# 实测主场略高，简化对称 ~0.014/min/side ≈ 0.000233/sec/side
# Poisson var ≈ mean，所以每秒方差 ≈ 0.000233
_SOCCER_GOAL_RATE_PER_SEC = 0.000233


def _soccer_ml_lock(side: SportsMarketSide, game: LiveGameState) -> MathLockResult:
    """Soccer 整场 ML：Poisson 双方剩余进球计算 P(lead 反转)。

    简化：lead = h - a，需要对手净进 ≥ lead+1（HOME 视角）。
    双方剩余进球独立 Poisson，差服从 Skellam，用正态近似 std ≈ sqrt(λ_h+λ_a)。
    """
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY, SportsMarketSide.DRAW}:
        return MathLockResult(_ZERO, "soccer_ml", "unsupported_side", {})
    home = game.home_score
    away = game.away_score
    remaining = game.seconds_remaining
    if remaining is None or remaining <= 0:
        if side == SportsMarketSide.HOME:
            won = home > away
        elif side == SportsMarketSide.AWAY:
            won = away > home
        else:
            won = home == away
        return MathLockResult(_ONE if won else _ZERO, "soccer_ml", "no_remaining_time",
                              {"home": home, "away": away})
    # 剩余进球期望 = 单边率 × 剩余秒
    lam = _SOCCER_GOAL_RATE_PER_SEC * remaining
    # Skellam 标准差 ≈ sqrt(2 × lam)
    sigma = math.sqrt(2.0 * lam)
    if sigma <= 0.01:
        # 时间太短，按当前比分判定
        if side == SportsMarketSide.HOME:
            return MathLockResult(_ONE if home > away else _ZERO, "soccer_ml",
                                  "negligible_remaining", {"home": home, "away": away})
        if side == SportsMarketSide.AWAY:
            return MathLockResult(_ONE if away > home else _ZERO, "soccer_ml",
                                  "negligible_remaining", {"home": home, "away": away})
        return MathLockResult(_ONE if home == away else _ZERO, "soccer_ml",
                              "negligible_remaining", {"home": home, "away": away})
    lead = home - away
    if side == SportsMarketSide.HOME:
        if lead <= 0:
            return MathLockResult(_ZERO, "soccer_ml", "not_leading", {"lead": lead})
        # P(剩余 goals_a - goals_h >= lead) → Skellam tail
        z = lead / sigma
        p_reversal_or_draw = 0.5 * math.erfc(z / math.sqrt(2.0))
        lock = max(0.0, min(1.0, 1.0 - p_reversal_or_draw))
    elif side == SportsMarketSide.AWAY:
        if lead >= 0:
            return MathLockResult(_ZERO, "soccer_ml", "not_leading", {"lead": -lead})
        z = (-lead) / sigma
        p_reversal_or_draw = 0.5 * math.erfc(z / math.sqrt(2.0))
        lock = max(0.0, min(1.0, 1.0 - p_reversal_or_draw))
    else:  # DRAW
        # 平局：当前已平局且剩余时间 Skellam=0 概率
        # 简化：P(no goals scored) × P(scored evenly) — 极小
        # 直接用 Poisson(2λ) P(N=0) 作下界
        from math import exp
        p_no_goals = exp(-2.0 * lam)
        if home == away:
            lock = p_no_goals  # 当前平局且没人再进球
        else:
            lock = 0.0  # 当前非平局，且剩余很难恰好抹平
    return MathLockResult(
        Decimal(str(round(lock, 4))), "soccer_ml", "live_estimate",
        {"home": home, "away": away, "remaining_seconds": remaining, "lambda_per_side": round(lam, 4)},
    )


def _soccer_totals_lock(
    side: SportsMarketSide, line: Decimal, game: LiveGameState
) -> MathLockResult:
    """Soccer Totals：双边总进球 Poisson(2λ) 计算 P(剩余进球 ≥ need)。"""
    if side not in {SportsMarketSide.OVER, SportsMarketSide.UNDER}:
        return MathLockResult(_ZERO, "soccer_totals", "unsupported_side", {})
    total = Decimal(int(game.total_score))
    remaining = game.seconds_remaining
    if remaining is None or remaining <= 0:
        won = (total > line) if side == SportsMarketSide.OVER else (total < line)
        return MathLockResult(_ONE if won else _ZERO, "soccer_totals", "game_ended",
                              {"total": str(total), "line": str(line)})
    if side == SportsMarketSide.OVER and total > line:
        return MathLockResult(_ONE, "soccer_totals", "already_over_win", {})
    if side == SportsMarketSide.UNDER and total >= line:
        return MathLockResult(_ZERO, "soccer_totals", "already_over_lose", {})
    lam_total = 2.0 * _SOCCER_GOAL_RATE_PER_SEC * remaining
    need = int(float(line) - float(total)) + (1 if side == SportsMarketSide.OVER else 0)
    # P(Poisson(lam_total) >= need)
    from math import exp, factorial
    p_under_need = sum(
        (lam_total ** k) * exp(-lam_total) / factorial(k) for k in range(max(0, need))
    )
    p_at_least_need = 1.0 - p_under_need
    if side == SportsMarketSide.OVER:
        lock = max(0.0, min(1.0, p_at_least_need))
    else:
        lock = max(0.0, min(1.0, 1.0 - p_at_least_need))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "soccer_totals", "live_estimate",
        {"total": str(total), "line": str(line), "remaining_seconds": remaining,
         "lambda_total": round(lam_total, 4), "need_goals": need,
         "p_at_least_need": round(p_at_least_need, 4)},
    )


def _soccer_spreads_lock(
    side: SportsMarketSide, line: Decimal, game: LiveGameState
) -> MathLockResult:
    """Soccer Spreads (Asian handicap)：lead+handicap 正态近似。"""
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return MathLockResult(_ZERO, "soccer_spreads", "unsupported_side", {})
    raw_lead = game.score_diff_for(side)
    adjusted = float(raw_lead) + (float(line) if side == SportsMarketSide.HOME else -float(line))
    remaining = game.seconds_remaining
    if remaining is None or remaining <= 0:
        return MathLockResult(_ONE if adjusted > 0 else _ZERO, "soccer_spreads",
                              "no_remaining_time", {"adjusted_lead": adjusted})
    if adjusted <= 0:
        return MathLockResult(_ZERO, "soccer_spreads", "not_leading_after_handicap",
                              {"adjusted_lead": adjusted})
    lam = _SOCCER_GOAL_RATE_PER_SEC * remaining
    sigma = math.sqrt(2.0 * lam)
    if sigma <= 0.01:
        return MathLockResult(_ONE, "soccer_spreads", "negligible_remaining",
                              {"adjusted_lead": adjusted})
    z = adjusted / sigma
    p_reversal = 0.5 * math.erfc(z / math.sqrt(2.0))
    lock = max(0.0, min(1.0, 1.0 - p_reversal))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "soccer_spreads", "live_estimate",
        {"raw_lead": raw_lead, "line": str(line), "adjusted_lead": adjusted,
         "remaining_seconds": remaining, "p_reversal": round(p_reversal, 4)},
    )


# ---------- Tennis (ATP / WTA / ITF) ----------
# Tennis 锁定基于 set/game 嵌套，无 seconds_remaining 直接用。
# 简化：用 sets_won_diff + 剩余 sets/games 估
def _tennis_ml_lock(side: SportsMarketSide, game: LiveGameState) -> MathLockResult:
    """Tennis 整场 ML：基于 sets 比分锁定。

    Best-of-3：先赢 2 set；best-of-5：先赢 3 set。
    我方 sets_won × 剩余 sets 不可能让对手追平 → 完全锁定。
    """
    state = game.tennis_state
    if state is None:
        return MathLockResult(_ZERO, "tennis_ml", "missing_tennis_state", {})
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return MathLockResult(_ZERO, "tennis_ml", "unsupported_side", {})
    my_sets = state.home_sets_won if side == SportsMarketSide.HOME else state.away_sets_won
    opp_sets = state.away_sets_won if side == SportsMarketSide.HOME else state.home_sets_won
    if my_sets is None or opp_sets is None:
        return MathLockResult(_ZERO, "tennis_ml", "missing_sets", {})
    best_of = state.best_of or 3
    need_sets = (best_of // 2) + 1
    if my_sets >= need_sets:
        return MathLockResult(_ONE, "tennis_ml", "match_already_won",
                              {"my_sets": my_sets, "opp_sets": opp_sets, "best_of": best_of})
    if opp_sets >= need_sets:
        return MathLockResult(_ZERO, "tennis_ml", "match_already_lost",
                              {"my_sets": my_sets, "opp_sets": opp_sets, "best_of": best_of})
    # 剩余可能性：opp 还需要 (need_sets - opp_sets) sets 才能赢
    opp_need = need_sets - opp_sets
    my_need = need_sets - my_sets
    remaining_sets = best_of - my_sets - opp_sets
    if opp_need > remaining_sets:
        return MathLockResult(_ONE, "tennis_ml", "opp_cannot_win",
                              {"my_sets": my_sets, "opp_sets": opp_sets, "best_of": best_of})
    # 简化：每个剩余 set 我方赢概率 = 0.55（领先方略占优）
    # 实际 = sigma(rating diff + serve advantage)，暂粗估
    p_win_set = 0.55 if my_sets > opp_sets else 0.45
    # 用动态规划算 P(先赢 my_need sets，对手先赢 opp_need 之前)
    # 简化：贝叶斯近似
    from math import comb
    p_win_match = 0.0
    for opp_wins in range(opp_need):
        # 我方赢 my_need + opp_wins 局中我赢 my_need
        n_games = my_need + opp_wins
        p_win_match += comb(n_games - 1, opp_wins) * (p_win_set ** my_need) * ((1 - p_win_set) ** opp_wins)
    lock = max(0.0, min(1.0, p_win_match))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "tennis_ml", "live_estimate",
        {"my_sets": my_sets, "opp_sets": opp_sets, "best_of": best_of,
         "p_win_set": p_win_set, "p_win_match": round(p_win_match, 4)},
    )


def _tennis_totals_lock(
    side: SportsMarketSide, line: Decimal, game: LiveGameState
) -> MathLockResult:
    """Tennis Total Games：当前已打 games + 剩余 sets/games 估算。

    简化：剩余 sets × 平均 games/set (~10) 估剩余总 games。
    """
    state = game.tennis_state
    if state is None:
        return MathLockResult(_ZERO, "tennis_totals", "missing_tennis_state", {})
    if side not in {SportsMarketSide.OVER, SportsMarketSide.UNDER}:
        return MathLockResult(_ZERO, "tennis_totals", "unsupported_side", {})
    games_played = (state.home_total_games or 0) + (state.away_total_games or 0)
    best_of = state.best_of or 3
    need_sets = (best_of // 2) + 1
    sets_played = (state.home_sets_won or 0) + (state.away_sets_won or 0)
    # 剩余 sets：可能 0（已结束）到 (best_of - sets_played)
    min_remaining_sets = max(0, need_sets - max(state.home_sets_won or 0, state.away_sets_won or 0))
    max_remaining_sets = best_of - sets_played
    if max_remaining_sets <= 0:
        won = (games_played > line) if side == SportsMarketSide.OVER else (games_played < line)
        return MathLockResult(_ONE if won else _ZERO, "tennis_totals", "match_ended",
                              {"games_played": games_played, "line": str(line)})
    # 平均每 set 10 games (含 tiebreak ~13)
    avg_games_per_set = 10.0
    expected_remaining = (min_remaining_sets + max_remaining_sets) / 2.0 * avg_games_per_set
    expected_total = games_played + expected_remaining
    # 简化：用 normal approx，sigma=每 set ~4 games std
    sigma = math.sqrt(max_remaining_sets) * 4.0
    if sigma <= 0.01:
        won = (games_played > line) if side == SportsMarketSide.OVER else (games_played < line)
        return MathLockResult(_ONE if won else _ZERO, "tennis_totals", "negligible_remaining",
                              {"games_played": games_played, "line": str(line)})
    need = float(line) - expected_total
    z = need / sigma
    p_above_line = 0.5 * math.erfc(z / math.sqrt(2.0))
    if side == SportsMarketSide.OVER:
        lock = max(0.0, min(1.0, p_above_line))
    else:
        lock = max(0.0, min(1.0, 1.0 - p_above_line))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "tennis_totals", "live_estimate",
        {"games_played": games_played, "line": str(line),
         "expected_total": round(expected_total, 2), "sigma": round(sigma, 2),
         "p_above_line": round(p_above_line, 4)},
    )


# ---------- Hockey (NHL / KHL) ----------
# 单边进球率：~3 goals/team/60min ≈ 0.000833 goals/sec
_HOCKEY_GOAL_RATE_PER_SEC = 0.000833


def _hockey_ml_lock(side: SportsMarketSide, game: LiveGameState) -> MathLockResult:
    """Hockey ML：Skellam 双边剩余进球差。"""
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return MathLockResult(_ZERO, "hockey_ml", "unsupported_side", {})
    lead = game.score_diff_for(side)
    if lead <= 0:
        return MathLockResult(_ZERO, "hockey_ml", "not_leading", {"lead": lead})
    remaining = game.seconds_remaining
    if remaining is None or remaining <= 0:
        return MathLockResult(_ONE, "hockey_ml", "no_remaining_time", {"lead": lead})
    lam = _HOCKEY_GOAL_RATE_PER_SEC * remaining
    sigma = math.sqrt(2.0 * lam)
    if sigma <= 0.01:
        return MathLockResult(_ONE, "hockey_ml", "negligible_remaining", {"lead": lead})
    z = lead / sigma
    p_reversal = 0.5 * math.erfc(z / math.sqrt(2.0))
    lock = max(0.0, min(1.0, 1.0 - p_reversal))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "hockey_ml", "live_estimate",
        {"lead": lead, "remaining_seconds": remaining, "p_reversal": round(p_reversal, 4)},
    )


def _hockey_totals_lock(
    side: SportsMarketSide, line: Decimal, game: LiveGameState
) -> MathLockResult:
    """Hockey Totals：双边总进球 Poisson(2λ)。"""
    if side not in {SportsMarketSide.OVER, SportsMarketSide.UNDER}:
        return MathLockResult(_ZERO, "hockey_totals", "unsupported_side", {})
    total = Decimal(int(game.total_score))
    remaining = game.seconds_remaining
    if remaining is None or remaining <= 0:
        won = (total > line) if side == SportsMarketSide.OVER else (total < line)
        return MathLockResult(_ONE if won else _ZERO, "hockey_totals", "game_ended",
                              {"total": str(total), "line": str(line)})
    if side == SportsMarketSide.OVER and total > line:
        return MathLockResult(_ONE, "hockey_totals", "already_over_win", {})
    if side == SportsMarketSide.UNDER and total >= line:
        return MathLockResult(_ZERO, "hockey_totals", "already_over_lose", {})
    lam_total = 2.0 * _HOCKEY_GOAL_RATE_PER_SEC * remaining
    need = int(float(line) - float(total)) + (1 if side == SportsMarketSide.OVER else 0)
    from math import exp, factorial
    p_under_need = sum(
        (lam_total ** k) * exp(-lam_total) / factorial(k) for k in range(max(0, need))
    )
    p_at_least = 1.0 - p_under_need
    lock = p_at_least if side == SportsMarketSide.OVER else (1.0 - p_at_least)
    lock = max(0.0, min(1.0, lock))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "hockey_totals", "live_estimate",
        {"total": str(total), "line": str(line), "remaining_seconds": remaining,
         "lambda_total": round(lam_total, 4), "p_at_least": round(p_at_least, 4)},
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
    """统一数学锁定评估。按 (market_type, sport) 分派；通用兜底覆盖所有盘口。

    优先级：sport-specific 专属公式 > generic_*_lock 兜底。
    无 sport 信息或 variance 表无该 sport → 返回 unsupported（lock_probability=0），
    strategy 可 fallback 到 goalserve_implied / microprice。
    """

    if game is None:
        return MathLockResult(_ZERO, "no_game", "missing_live_game_state", {})
    if game.status == LiveGameStatus.ENDED:
        return MathLockResult(
            _ONE, "game_ended", "game_already_ended",
            {"home_score": game.home_score, "away_score": game.away_score},
        )

    sport = (game.sport or "").strip().lower()

    # ===== Baseball (MLB/KBO/NPB) =====
    if game.baseball_state is not None:
        if market_type == SportsMarketType.TOTALS and line is not None:
            return _mlb_totals_lock(side, line, game)
        if market_type == SportsMarketType.MONEYLINE:
            return _baseball_moneyline_lock(side, game)

    # ===== Soccer (整场 + halftime prop) =====
    if game.soccer_state is not None or sport == "soccer":
        if market_type == SportsMarketType.BINARY_PROP:
            slug = (market_slug or "").lower()
            for direction in ("home", "draw", "away"):
                if slug.endswith(f"halftime-result-{direction}"):
                    return _soccer_halftime_lock(side, direction, game)
        if market_type == SportsMarketType.MONEYLINE:
            return _soccer_ml_lock(side, game)
        if market_type == SportsMarketType.TOTALS and line is not None:
            return _soccer_totals_lock(side, line, game)
        if market_type == SportsMarketType.SPREADS and line is not None:
            return _soccer_spreads_lock(side, line, game)

    # ===== Basketball (NBA/WNBA/CBA) =====
    if game.basketball_state is not None or sport == "basketball":
        if market_type == SportsMarketType.MONEYLINE:
            return _basketball_ml_lock(side, game)
        if market_type == SportsMarketType.TOTALS and line is not None:
            return _basketball_totals_lock(side, line, game)
        if market_type == SportsMarketType.SPREADS and line is not None:
            return _basketball_spreads_lock(side, line, game)

    # ===== Tennis (ATP/WTA/ITF) =====
    if game.tennis_state is not None or sport == "tennis":
        if market_type == SportsMarketType.MONEYLINE:
            return _tennis_ml_lock(side, game)
        if market_type == SportsMarketType.TOTALS and line is not None:
            return _tennis_totals_lock(side, line, game)

    # ===== Hockey (NHL/KHL) =====
    if sport == "hockey":
        if market_type == SportsMarketType.MONEYLINE:
            return _hockey_ml_lock(side, game)
        if market_type == SportsMarketType.TOTALS and line is not None:
            return _hockey_totals_lock(side, line, game)

    return _UNSUPPORTED


__all__ = [
    "MathLockResult",
    "evaluate_math_lock",
]
