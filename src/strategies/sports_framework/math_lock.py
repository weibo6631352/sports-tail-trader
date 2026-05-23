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
    # 关键：halftime 盘口结算条件是**半场比分**，不是全场比分。半场结束后全场
    # 比分会继续变化（2H 进球），但 halftime-result 已锁定 = HT 那一刻的比分。
    # 用 state.home_halftime_score / away_halftime_score（半场结束时定格的比分）；
    # 仅在 first_half 进行中且尚无 halftime score 时退回 game.home_score 作近似。
    in_first_half = period == "first_half"
    if in_first_half:
        home = int(game.home_score or 0)
        away = int(game.away_score or 0)
    else:
        home = int(state.home_halftime_score or 0) if state.home_halftime_score is not None else int(game.home_score or 0)
        away = int(state.away_halftime_score or 0) if state.away_halftime_score is not None else int(game.away_score or 0)
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
# 通用辅助：剩余秒数估算
# ============================================================


def _estimate_remaining_seconds(game: LiveGameState) -> int | None:
    """按 sport + period + clock 估算剩余秒数，兜底 game.seconds_remaining=None 的场景。

    livescore feed 经常只给 "Inning 5" / "1st Half 32min" 这种文本，不给精确秒数。
    用经验时长估算让 math_lock 仍可基于"剩余时间"算 reversal 概率，而不是因为
    seconds_remaining=None 就误返 lock=1.0（"无剩余时间 = 锁定"）。

    返回 None 表示无法估算（无 period/clock 数据）→ 上层应 fallback。
    """

    sport = (game.sport or "").strip().lower()
    if game.seconds_remaining is not None and game.seconds_remaining > 0:
        return game.seconds_remaining

    # ----- Soccer (标准 90min: 1H 45 + 半场 15 + 2H 45 + injury ~3-5) -----
    if game.soccer_state is not None or sport == "soccer":
        ss = game.soccer_state
        if ss is None:
            return None
        period = (ss.period or "").lower()
        clock = ss.clock_minutes or 0
        if period == "first_half":
            # 剩 1H + 半场 + 2H + injury = (45-clock)*60 + 15*60 + 48*60
            return max(0, (108 - clock)) * 60
        if period == "halftime":
            return (45 + 3) * 60  # 半场+整个2H+injury
        if period == "second_half":
            return max(0, (48 - clock)) * 60  # 2H+injury
        if period in {"extra_time", "extra_time_first_half", "extra_time_second_half"}:
            return 15 * 60  # 加时单 half
        if period == "penalties":
            return 5 * 60
        return None

    # ----- Basketball (NBA 48min, 4 quarters × 12min; 加时 5min) -----
    if game.basketball_state is not None or sport == "basketball":
        bs = game.basketball_state
        if bs is None or bs.current_period is None:
            return None
        period = bs.current_period
        if period >= 5:  # OT
            return 5 * 60  # 单 OT 期，无法估剩余更细，给整 OT
        # 估当前节剩 6 分钟（节中间），后续节 12min/节
        remaining_quarters = 4 - period
        return (remaining_quarters * 12 + 6) * 60

    # ----- Hockey (NHL 60min, 3 periods × 20min; OT 5min) -----
    if sport == "hockey":
        # 暂用 game.period 文本（domain 无 hockey_state）
        period_str = (game.period or "").lower()
        if "overtime" in period_str or "ot" in period_str:
            return 5 * 60
        if "1st" in period_str or "first" in period_str:
            return (10 + 20 + 20) * 60  # 当节中间 + 后 2 节
        if "2nd" in period_str or "second" in period_str:
            return (10 + 20) * 60
        if "3rd" in period_str or "third" in period_str or "final" in period_str:
            return 10 * 60  # 当节中间
        return None

    # ----- Cricket/Rugby/AmFootball/Volleyball/Handball: 当前 generic 不支持 -----
    return None


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
    remaining = _estimate_remaining_seconds(game)
    if remaining is None:
        return MathLockResult(_ZERO, "basketball_ml", "missing_remaining_time", {"lead": lead})
    if remaining <= 0:
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
    remaining = _estimate_remaining_seconds(game)
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
    remaining = _estimate_remaining_seconds(game)
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
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return MathLockResult(_ZERO, "soccer_ml", "unsupported_side", {})
    home = game.home_score
    away = game.away_score
    remaining = _estimate_remaining_seconds(game)
    if remaining is None or remaining <= 0:
        if side == SportsMarketSide.HOME:
            won = home > away
        else:
            won = away > home
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
    remaining = _estimate_remaining_seconds(game)
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
    remaining = _estimate_remaining_seconds(game)
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
    remaining = _estimate_remaining_seconds(game)
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


def _soccer_btts_lock(side: SportsMarketSide, game: LiveGameState) -> MathLockResult:
    """Soccer BTTS (Both Teams To Score) Yes/No 锁定。

    Yes 锁定 = 双方都至少进 1 球 (home_score >= 1 AND away_score >= 1)。
    进行中按 Poisson 双边率算 P(任一方仍 = 0)。
    """
    if side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return MathLockResult(_ZERO, "soccer_btts", "unsupported_side", {})
    h = game.home_score
    a = game.away_score
    both_scored = h >= 1 and a >= 1
    remaining = _estimate_remaining_seconds(game)
    if remaining is None:
        return MathLockResult(_ZERO, "soccer_btts", "missing_remaining_time", {})
    if remaining <= 0 or game.status == LiveGameStatus.ENDED:
        # 比赛结束按当前比分定
        yes_won = both_scored
        return MathLockResult(
            _ONE if (yes_won if side == SportsMarketSide.YES else not yes_won) else _ZERO,
            "soccer_btts", "game_ended",
            {"home": h, "away": a, "both_scored": both_scored, "side": side.value},
        )
    if both_scored:
        return MathLockResult(
            _ONE if side == SportsMarketSide.YES else _ZERO,
            "soccer_btts", "both_already_scored",
            {"home": h, "away": a},
        )
    from math import exp
    lam = _SOCCER_GOAL_RATE_PER_SEC * remaining  # 单边期望
    # 哪边还没进 → 它进 ≥1 概率 = 1 - exp(-λ)
    if h == 0 and a == 0:
        p_each_scores = 1.0 - exp(-lam)
        p_btts_yes = p_each_scores ** 2  # 双方独立都进
    elif h == 0:
        p_btts_yes = 1.0 - exp(-lam)  # 只需 home 进
    else:  # a == 0
        p_btts_yes = 1.0 - exp(-lam)
    lock = p_btts_yes if side == SportsMarketSide.YES else (1.0 - p_btts_yes)
    return MathLockResult(
        Decimal(str(round(max(0.0, min(1.0, lock)), 4))),
        "soccer_btts", "live_estimate",
        {"home": h, "away": a, "remaining_seconds": remaining,
         "p_btts_yes": round(p_btts_yes, 4), "side": side.value},
    )


def _soccer_draw_ft_lock(side: SportsMarketSide, game: LiveGameState) -> MathLockResult:
    """Soccer Draw Yes/No (整场是否平局) — 用 Skellam(0) 算剩余进球差 = 0 概率。"""
    if side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return MathLockResult(_ZERO, "soccer_draw_ft", "unsupported_side", {})
    h = game.home_score
    a = game.away_score
    remaining = _estimate_remaining_seconds(game)
    if remaining is None:
        return MathLockResult(_ZERO, "soccer_draw_ft", "missing_remaining_time", {})
    if remaining <= 0 or game.status == LiveGameStatus.ENDED:
        is_draw = h == a
        won = is_draw if side == SportsMarketSide.YES else not is_draw
        return MathLockResult(
            _ONE if won else _ZERO, "soccer_draw_ft", "game_ended",
            {"home": h, "away": a, "is_draw": is_draw},
        )
    lam = _SOCCER_GOAL_RATE_PER_SEC * remaining
    # Skellam(λ_h, λ_a) 在 k 处的 pmf：用 Bessel modified function。简化：
    # P(Δgoals = current_diff) 用枚举 Poisson 双边 sum
    from math import exp, factorial
    current_diff = h - a
    # 我方需要剩余 (away - home) goals = current_diff
    # P(剩 home_goals = k, away_goals = k + current_diff)
    p_draw = 0.0
    max_k = max(3, int(lam * 5))  # 截断
    for k in range(0, max_k + 1):
        opp_k = k + current_diff
        if opp_k < 0:
            continue
        p_h = (lam ** k) * exp(-lam) / factorial(k)
        p_a = (lam ** opp_k) * exp(-lam) / factorial(opp_k) if opp_k <= max_k else 0
        p_draw += p_h * p_a
    lock = p_draw if side == SportsMarketSide.YES else (1.0 - p_draw)
    return MathLockResult(
        Decimal(str(round(max(0.0, min(1.0, lock)), 4))),
        "soccer_draw_ft", "live_estimate",
        {"home": h, "away": a, "current_diff": current_diff,
         "remaining_seconds": remaining, "lambda_per_side": round(lam, 4),
         "p_draw": round(p_draw, 4), "side": side.value},
    )


def _baseball_nrfi_lock(side: SportsMarketSide, game: LiveGameState) -> MathLockResult:
    """Baseball NRFI (No Run First Inning) Yes/No 锁定。

    Yes 锁定 = inning 1 双方都没得分。inning >= 2 + first inning runs == 0 → Yes 锁。
    inning 1 中: 用 Poisson 单半局 λ=0.55 算剩余半局任一进 ≥1 的反向概率。
    """
    if side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return MathLockResult(_ZERO, "baseball_nrfi", "unsupported_side", {})
    state = game.baseball_state
    if state is None or state.current_inning is None:
        return MathLockResult(_ZERO, "baseball_nrfi", "missing_baseball_state", {})
    inning = state.current_inning
    first_h_runs = (state.home_inning_runs or (None,))[0] if state.home_inning_runs else None
    first_a_runs = (state.away_inning_runs or (None,))[0] if state.away_inning_runs else None
    if inning >= 2:
        # First inning 已结束
        if first_h_runs is None or first_a_runs is None:
            # 数据缺：保守按 unsupported
            return MathLockResult(_ZERO, "baseball_nrfi", "missing_first_inning_runs", {})
        no_runs = first_h_runs == 0 and first_a_runs == 0
        won = no_runs if side == SportsMarketSide.YES else not no_runs
        return MathLockResult(
            _ONE if won else _ZERO, "baseball_nrfi", "first_inning_completed",
            {"first_h": first_h_runs, "first_a": first_a_runs, "no_runs": no_runs},
        )
    # inning == 1
    half = (state.inning_half or "top").lower()
    if (first_h_runs and first_h_runs >= 1) or (first_a_runs and first_a_runs >= 1):
        # 已经有得分 → NRFI 输
        return MathLockResult(
            _ZERO if side == SportsMarketSide.YES else _ONE,
            "baseball_nrfi", "run_already_scored_in_first",
            {"first_h": first_h_runs, "first_a": first_a_runs},
        )
    # 剩余半局：top → 1 (top remainder) + 1 (bottom); bottom → 1 (remainder)
    remaining_half = 1 if half == "bottom" else 2
    # P(单半局 0 runs) = _MLB_HALF_INNING_SCORE_DIST[0] = 0.70
    p_no_run_single = float(_MLB_HALF_INNING_SCORE_DIST[0])
    p_nrfi_yes = p_no_run_single ** remaining_half
    lock = p_nrfi_yes if side == SportsMarketSide.YES else (1.0 - p_nrfi_yes)
    return MathLockResult(
        Decimal(str(round(max(0.0, min(1.0, lock)), 4))),
        "baseball_nrfi", "live_estimate",
        {"inning": inning, "half": half, "remaining_half_innings_in_first": remaining_half,
         "p_nrfi_yes": round(p_nrfi_yes, 4), "side": side.value},
    )


def _basketball_period_ml_lock(
    side: SportsMarketSide, game: LiveGameState, scope_quarter: int | None = None
) -> MathLockResult:
    """篮球分节 ML（1H/Q1-4）：用对应节剩余秒数算 reversal。

    scope_quarter=None 时是 1H (Q1+Q2 合并)；否则是具体节。
    简化用 _BASKETBALL_VAR_PER_SECOND × scope_remaining_seconds。
    """
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return MathLockResult(_ZERO, "basketball_period_ml", "unsupported_side", {})
    bs = game.basketball_state
    if bs is None or bs.current_period is None:
        return MathLockResult(_ZERO, "basketball_period_ml", "missing_basketball_state", {})
    current = bs.current_period
    if scope_quarter is not None:
        # 单节：scope 节早于当前节 → 已结束按累计判（暂用 game 总 lead，粗）
        if current > scope_quarter:
            # 节已结束，按该节比分判（域里没分节比分则 unsupported）
            quarter_idx = scope_quarter - 1
            h_q = (bs.home_quarter_scores or ())[quarter_idx] if quarter_idx < len(bs.home_quarter_scores or ()) else None
            a_q = (bs.away_quarter_scores or ())[quarter_idx] if quarter_idx < len(bs.away_quarter_scores or ()) else None
            if h_q is None or a_q is None:
                return MathLockResult(_ZERO, "basketball_period_ml", "missing_quarter_score", {})
            won = (h_q > a_q) if side == SportsMarketSide.HOME else (a_q > h_q)
            return MathLockResult(
                _ONE if won else _ZERO, "basketball_period_ml", "quarter_ended",
                {"quarter": scope_quarter, "h_q": h_q, "a_q": a_q},
            )
        if current < scope_quarter:
            # 还没到该节：完全未知，返回 0.5 中性
            return MathLockResult(Decimal("0.5"), "basketball_period_ml", "quarter_not_started",
                                  {"quarter": scope_quarter, "current": current})
        # current == scope_quarter：用单节剩余秒
        remaining_in_q = 6 * 60  # 估当前节剩余 6min（节中间）
    else:
        # 1H：剩余 Q1+Q2 - 已打部分
        if current > 2:
            # 1H 已结束
            return MathLockResult(_ZERO, "basketball_period_ml", "first_half_ended_lockup_needed", {})
        remaining_in_q = (2 - current) * 12 * 60 + 6 * 60
    # 用 lead 算锁定（暂用整场 lead 当 1H/Q lead，粗）
    lead = game.score_diff_for(side)
    if lead <= 0:
        return MathLockResult(_ZERO, "basketball_period_ml", "not_leading_in_scope", {"lead": lead})
    var_diff = 2.0 * _BASKETBALL_VAR_PER_SECOND * remaining_in_q
    z = lead / math.sqrt(var_diff)
    p_reversal = 0.5 * math.erfc(z / math.sqrt(2.0))
    lock = max(0.0, min(1.0, 1.0 - p_reversal))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "basketball_period_ml", "live_estimate",
        {"scope": f"Q{scope_quarter}" if scope_quarter else "1H",
         "lead": lead, "remaining_seconds": remaining_in_q,
         "p_reversal": round(p_reversal, 4)},
    )


# ---------- Hockey Spreads ----------
def _hockey_spreads_lock(
    side: SportsMarketSide, line: Decimal, game: LiveGameState
) -> MathLockResult:
    """NHL/IIHF Spreads (puck line)：让分后净 lead Skellam 近似 reversal。"""
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return MathLockResult(_ZERO, "hockey_spreads", "unsupported_side", {})
    raw_lead = game.score_diff_for(side)
    adjusted = float(raw_lead) + (float(line) if side == SportsMarketSide.HOME else -float(line))
    remaining = _estimate_remaining_seconds(game)
    if remaining is None:
        return MathLockResult(_ZERO, "hockey_spreads", "missing_remaining_time", {})
    if remaining <= 0:
        return MathLockResult(
            _ONE if adjusted > 0 else _ZERO, "hockey_spreads", "no_remaining_time",
            {"adjusted_lead": adjusted},
        )
    if adjusted <= 0:
        return MathLockResult(_ZERO, "hockey_spreads", "not_leading_after_handicap",
                              {"adjusted_lead": adjusted, "raw_lead": raw_lead, "line": str(line)})
    lam = _HOCKEY_GOAL_RATE_PER_SEC * remaining
    sigma = math.sqrt(2.0 * lam)
    if sigma <= 0.01:
        return MathLockResult(_ONE, "hockey_spreads", "negligible_remaining", {"adjusted_lead": adjusted})
    z = adjusted / sigma
    p_reversal = 0.5 * math.erfc(z / math.sqrt(2.0))
    lock = max(0.0, min(1.0, 1.0 - p_reversal))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "hockey_spreads", "live_estimate",
        {"raw_lead": raw_lead, "line": str(line), "adjusted_lead": adjusted,
         "remaining_seconds": remaining, "p_reversal": round(p_reversal, 4)},
    )


# ---------- Tennis Set Winner (current set) ----------
def _tennis_set_winner_lock(side: SportsMarketSide, game: LiveGameState) -> MathLockResult:
    """Tennis 当前盘 set winner：用当前 set 内 games 比分 + best-of-set 二项概率。

    Set 标准 best-of-13 games（先到 6 局且领先 2+）。简化：用 current_set_games
    估算 P(剩余局数我方先到 6)。
    """
    state = game.tennis_state
    if state is None:
        return MathLockResult(_ZERO, "tennis_set_winner", "missing_tennis_state", {})
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return MathLockResult(_ZERO, "tennis_set_winner", "unsupported_side", {})
    my_games = state.home_current_set_games if side == SportsMarketSide.HOME else state.away_current_set_games
    opp_games = state.away_current_set_games if side == SportsMarketSide.HOME else state.home_current_set_games
    if my_games is None or opp_games is None:
        return MathLockResult(_ZERO, "tennis_set_winner", "missing_set_games", {})
    # Set 先到 6 局且领先 2+，否则 7-5 / 7-6 (tiebreak)
    if my_games >= 6 and my_games >= opp_games + 2:
        return MathLockResult(_ONE, "tennis_set_winner", "set_already_won",
                              {"my_games": my_games, "opp_games": opp_games})
    if opp_games >= 6 and opp_games >= my_games + 2:
        return MathLockResult(_ZERO, "tennis_set_winner", "set_already_lost",
                              {"my_games": my_games, "opp_games": opp_games})
    # 简化：用 my_games / (my+opp) 历史频率作单局胜率
    games_played = my_games + opp_games
    p_per_game = (my_games / games_played) if games_played > 0 else 0.5
    p_per_game = max(0.3, min(0.7, p_per_game))  # cap 避免极端
    # 距离 set 胜：先到 6（若双方 ≥5 则到 7）
    target = 7 if max(my_games, opp_games) >= 6 else 6
    my_need = max(0, target - my_games)
    opp_need = max(0, target - opp_games)
    # 类似 series 公式：P(我方先到 my_need 局)
    from math import comb
    p_win = 0.0
    for opp_wins in range(opp_need):
        n_games = my_need + opp_wins
        p_win += comb(n_games - 1, opp_wins) * (p_per_game ** my_need) * ((1 - p_per_game) ** opp_wins)
    lock = max(0.0, min(1.0, p_win))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "tennis_set_winner", "live_estimate",
        {"my_games": my_games, "opp_games": opp_games, "target": target,
         "p_per_game": round(p_per_game, 4), "p_win_set": round(p_win, 4)},
    )


# ---------- NFL (American Football) ----------
# NFL 每秒得分方差：~45 pts/match/60min ≈ 0.0125 pts/sec/team
# 单次得分大小 3/6/7/8 → σ² ≈ 0.05 per sec/side
_NFL_VAR_PER_SECOND = 0.05


def _nfl_ml_lock(side: SportsMarketSide, game: LiveGameState) -> MathLockResult:
    """NFL ML：lead × 剩余秒数双边正态近似 reversal。"""
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return MathLockResult(_ZERO, "nfl_ml", "unsupported_side", {})
    lead = game.score_diff_for(side)
    if lead <= 0:
        return MathLockResult(_ZERO, "nfl_ml", "not_leading", {"lead": lead})
    remaining = _estimate_remaining_seconds(game)
    if remaining is None:
        return MathLockResult(_ZERO, "nfl_ml", "missing_remaining_time", {})
    if remaining <= 0:
        return MathLockResult(_ONE, "nfl_ml", "no_remaining_time", {"lead": lead})
    var_diff = 2.0 * _NFL_VAR_PER_SECOND * remaining
    z = lead / math.sqrt(var_diff) if var_diff > 0 else 0
    p_reversal = 0.5 * math.erfc(z / math.sqrt(2.0))
    lock = max(0.0, min(1.0, 1.0 - p_reversal))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "nfl_ml", "live_estimate",
        {"lead": lead, "remaining_seconds": remaining, "p_reversal": round(p_reversal, 4)},
    )


def _nfl_totals_lock(side: SportsMarketSide, line: Decimal, game: LiveGameState) -> MathLockResult:
    """NFL Totals：双边总得分正态近似。"""
    if side not in {SportsMarketSide.OVER, SportsMarketSide.UNDER}:
        return MathLockResult(_ZERO, "nfl_totals", "unsupported_side", {})
    total = Decimal(int(game.total_score))
    remaining = _estimate_remaining_seconds(game)
    if remaining is None:
        return MathLockResult(_ZERO, "nfl_totals", "missing_remaining_time", {})
    if remaining <= 0:
        won = (total > line) if side == SportsMarketSide.OVER else (total < line)
        return MathLockResult(_ONE if won else _ZERO, "nfl_totals", "game_ended",
                              {"total": str(total), "line": str(line)})
    if side == SportsMarketSide.OVER and total > line:
        return MathLockResult(_ONE, "nfl_totals", "already_over_win", {})
    if side == SportsMarketSide.UNDER and total >= line:
        return MathLockResult(_ZERO, "nfl_totals", "already_over_lose", {})
    var_total = 2.0 * _NFL_VAR_PER_SECOND * remaining
    need = float(line) - float(total) + (0.5 if side == SportsMarketSide.OVER else 0.0)
    z = need / math.sqrt(var_total) if var_total > 0 else 0
    p_break = 0.5 * math.erfc(z / math.sqrt(2.0))
    lock = p_break if side == SportsMarketSide.OVER else (1.0 - p_break)
    return MathLockResult(
        Decimal(str(round(max(0.0, min(1.0, lock)), 4))), "nfl_totals", "live_estimate",
        {"total": str(total), "line": str(line), "remaining_seconds": remaining,
         "p_break": round(p_break, 4)},
    )


def _nfl_spreads_lock(side: SportsMarketSide, line: Decimal, game: LiveGameState) -> MathLockResult:
    """NFL Spreads：让分后净 lead 正态近似。"""
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return MathLockResult(_ZERO, "nfl_spreads", "unsupported_side", {})
    raw_lead = game.score_diff_for(side)
    adjusted = float(raw_lead) + (float(line) if side == SportsMarketSide.HOME else -float(line))
    remaining = _estimate_remaining_seconds(game)
    if remaining is None:
        return MathLockResult(_ZERO, "nfl_spreads", "missing_remaining_time", {})
    if remaining <= 0:
        return MathLockResult(_ONE if adjusted > 0 else _ZERO, "nfl_spreads", "no_remaining_time",
                              {"adjusted_lead": adjusted})
    if adjusted <= 0:
        return MathLockResult(_ZERO, "nfl_spreads", "not_leading_after_handicap",
                              {"adjusted_lead": adjusted})
    var_diff = 2.0 * _NFL_VAR_PER_SECOND * remaining
    z = adjusted / math.sqrt(var_diff) if var_diff > 0 else 0
    p_reversal = 0.5 * math.erfc(z / math.sqrt(2.0))
    lock = max(0.0, min(1.0, 1.0 - p_reversal))
    return MathLockResult(
        Decimal(str(round(lock, 4))), "nfl_spreads", "live_estimate",
        {"raw_lead": raw_lead, "line": str(line), "adjusted_lead": adjusted,
         "remaining_seconds": remaining, "p_reversal": round(p_reversal, 4)},
    )


def _cricket_chase_lock(side: SportsMarketSide, game: LiveGameState) -> MathLockResult:
    """Cricket 第二局 chase ML：剩余 balls + wickets + 目标差。

    板球追分场景：batting side 需要 runs ≥ target。简化模型——剩余球数 × 平均
    run rate (1 run/ball T20 保守) 估期望额外 runs，wickets_left 作降权。
    """
    state = game.cricket_state
    if state is None or state.target is None:
        return MathLockResult(_ZERO, "cricket_ml", "missing_cricket_target", {})
    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return MathLockResult(_ZERO, "cricket_ml", "unsupported_side", {})
    runs = state.runs or 0
    target = state.target
    need = target - runs
    rem_balls = state.required_balls or 0
    wickets_left = max(0, 10 - (state.wickets or 0))
    batting_side = (state.batting_side or "").lower()
    side_is_batting = (
        (side == SportsMarketSide.HOME and batting_side == "home")
        or (side == SportsMarketSide.AWAY and batting_side == "away")
    )
    if need <= 0:
        return MathLockResult(
            _ONE if side_is_batting else _ZERO,
            "cricket_ml", "chase_target_reached",
            {"runs": runs, "target": target, "side": side.value},
        )
    if rem_balls <= 0 or wickets_left <= 0:
        return MathLockResult(
            _ZERO if side_is_batting else _ONE,
            "cricket_ml", "no_remaining_balls_or_wickets",
            {"runs": runs, "target": target, "rem_balls": rem_balls, "wickets_left": wickets_left},
        )
    avg_runs_per_ball = 1.0
    var_per_ball = 2.0
    projected = rem_balls * avg_runs_per_ball
    std = math.sqrt(rem_balls * var_per_ball)
    z = (projected - need) / std if std > 0 else 0
    p_chase = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    wicket_factor = min(1.0, wickets_left / 5.0)
    p_batting_win = max(0.0, min(1.0, p_chase * wicket_factor))
    lock = p_batting_win if side_is_batting else (1.0 - p_batting_win)
    return MathLockResult(
        Decimal(str(round(lock, 4))), "cricket_ml", "live_estimate",
        {"runs": runs, "target": target, "rem_balls": rem_balls,
         "wickets_left": wickets_left, "p_chase": round(p_chase, 4),
         "wicket_factor": round(wicket_factor, 4)},
    )


def _hockey_totals_lock(
    side: SportsMarketSide, line: Decimal, game: LiveGameState
) -> MathLockResult:
    """Hockey Totals：双边总进球 Poisson(2λ)。"""
    if side not in {SportsMarketSide.OVER, SportsMarketSide.UNDER}:
        return MathLockResult(_ZERO, "hockey_totals", "unsupported_side", {})
    total = Decimal(int(game.total_score))
    remaining = _estimate_remaining_seconds(game)
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

# 子段 scope slug 标记（首段 / 半场 / 分节 / 分局 / 特殊 prop）。命中即视为
# 子段盘口，必须有专用公式才能跑 math_lock，否则返回 sub_scope_no_dedicated_lock。
_SUB_SCOPE_TOKENS = (
    "first-set", "second-set", "third-set", "fourth-set", "fifth-set",
    "set-1", "set-2", "set-3", "set-4", "set-5",
    "first-half", "second-half", "1st-half", "2nd-half",
    "1h-", "2h-",  # nba/basketball 1H/2H 简写（如 "nba-...-1h-total-110pt5"）
    "first-quarter", "second-quarter", "third-quarter", "fourth-quarter",
    "1st-quarter", "2nd-quarter", "3rd-quarter", "4th-quarter",
    "q1-", "q2-", "q3-", "q4-",  # NBA Q1/Q2/Q3/Q4 简写
    "first-period", "second-period", "third-period",
    "1st-period", "2nd-period", "3rd-period",
    "first-inning", "second-inning", "third-inning", "fourth-inning",
    "fifth-inning",
    "exact-score", "correct-score", "exact-",
    "anytime-goalscorer", "first-goalscorer", "last-goalscorer",
    "player-", "to-score", "to-win-",
    "halftime", "ht-",  # halftime / HT scope（不论 result/total/spread）
)


def _is_sub_scope_without_dedicated_lock(
    slug_lc: str,
    market_type: SportsMarketType,
    sport: str,
    game: LiveGameState | None,
) -> bool:
    """slug 是子段 scope 但当前没有专用 math_lock 公式 → True（视为 unsupported）。

    已有专用公式的 sub-scope 不算 unsupported（让后续 dispatch 走专用 fn）：
    - soccer + BINARY_PROP + halftime-result-{home,draw,away} → _soccer_halftime_lock
    - soccer + BINARY_PROP + btts/both-teams-to-score → _soccer_btts_lock
    - tennis + BINARY_PROP + set-winner → _tennis_set_winner_lock
    - mlb + BINARY_PROP + nrfi → _baseball_nrfi_lock
    """
    if not slug_lc or not any(t in slug_lc for t in _SUB_SCOPE_TOKENS):
        return False
    # 已有专用公式的 sub-scope（dispatch 后会走专用 fn）放行
    if market_type == SportsMarketType.BINARY_PROP:
        if sport == "soccer" and (
            "halftime-result-" in slug_lc
            or "btts" in slug_lc
            or "both-teams-to-score" in slug_lc
            or "both-teams-score" in slug_lc
        ):
            return False
        if sport == "tennis" and (
            "set-winner" in slug_lc or "current-set" in slug_lc
        ):
            return False
        if game is not None and game.baseball_state is not None and "nrfi" in slug_lc:
            return False
        if game is not None and game.baseball_state is not None and "first-inning-no-run" in slug_lc:
            return False
        if game is not None and game.baseball_state is not None and "no-runs-first-inning" in slug_lc:
            return False
    return True


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

    # ===== 子段 scope 守门 =====
    # 子段盘口（first-set-total / 1H total / quarter ML / first-inning prop 等）
    # 必须走专用公式（基于子段比分 + 子段剩余时间）。若没有专用公式，绝不能落到
    # 整场 _tennis_totals_lock / _basketball_ml_lock 等——它们用整场比分计算，对
    # first-set 之类已结束子段会算出完全错误的 lock_prob，导致 entry math_lock veto
    # 错杀已锁定赢方（或漏放过已锁定输方）。
    # 已有专用公式的 sub-scope（slug 命中下方专属分派后会走专用 fn）：
    #   - soccer halftime-result-{home,draw,away} → _soccer_halftime_lock
    #   - soccer BTTS / draw-FT → _soccer_btts_lock / _soccer_draw_ft_lock
    #   - tennis set-winner → _tennis_set_winner_lock
    #   - mlb NRFI → _baseball_nrfi_lock
    # 其余 sub-scope（tennis first-set-total / basketball 1H / quarter 等）尚无
    # 专用 math_lock 公式 → 在分派前先返回 UNSUPPORTED，让上层 evaluator 自己
    # 处理（odds_gap 顶层 _math_lock_veto 对 unsupported 放行，§17 不放过可盈利市场）。
    slug_lc = (market_slug or "").lower()
    if _is_sub_scope_without_dedicated_lock(slug_lc, market_type, sport, game):
        return MathLockResult(_ZERO, "sub_scope", "sub_scope_no_dedicated_lock", {"slug": market_slug})

    # ===== Baseball (MLB/KBO/NPB) =====
    if game.baseball_state is not None:
        slug_lc = (market_slug or "").lower()
        if market_type == SportsMarketType.BINARY_PROP and (
            "nrfi" in slug_lc or "no-runs-first-inning" in slug_lc or "first-inning-no-run" in slug_lc
        ):
            return _baseball_nrfi_lock(side, game)
        if market_type == SportsMarketType.TOTALS and line is not None:
            return _mlb_totals_lock(side, line, game)
        if market_type == SportsMarketType.MONEYLINE:
            return _baseball_moneyline_lock(side, game)

    # ===== Soccer (整场 + halftime + BTTS + Draw FT prop) =====
    if game.soccer_state is not None or sport == "soccer":
        if market_type == SportsMarketType.BINARY_PROP:
            slug = (market_slug or "").lower()
            for direction in ("home", "draw", "away"):
                if slug.endswith(f"halftime-result-{direction}"):
                    return _soccer_halftime_lock(side, direction, game)
            # BTTS: slug 含 "both-teams-to-score" / "btts"
            if "btts" in slug or "both-teams-to-score" in slug or "both-teams-score" in slug:
                return _soccer_btts_lock(side, game)
            # Draw Yes/No: slug 以 -draw / -tie 结尾且非 halftime
            if (slug.endswith("-draw") or slug.endswith("-tie") or "draw-no-bet" not in slug and "-draw-" in slug) and "halftime" not in slug:
                return _soccer_draw_ft_lock(side, game)
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

    # ===== Cricket (T20/ODI chase 模型) =====
    if game.cricket_state is not None or sport == "cricket":
        if market_type == SportsMarketType.MONEYLINE:
            return _cricket_chase_lock(side, game)

    # ===== Hockey (NHL/KHL/IIHF) =====
    if sport == "hockey" or sport == "ice-hockey":
        if market_type == SportsMarketType.MONEYLINE:
            return _hockey_ml_lock(side, game)
        if market_type == SportsMarketType.TOTALS and line is not None:
            return _hockey_totals_lock(side, line, game)
        if market_type == SportsMarketType.SPREADS and line is not None:
            return _hockey_spreads_lock(side, line, game)

    # ===== Tennis set winner (binary) =====
    if (game.tennis_state is not None or sport == "tennis") and market_type == SportsMarketType.BINARY_PROP:
        slug_lc = (market_slug or "").lower()
        if "set-winner" in slug_lc or "current-set" in slug_lc or "set" in slug_lc and "tiebreak" not in slug_lc:
            return _tennis_set_winner_lock(side, game)

    # ===== American Football (NFL / College) =====
    if sport in {"amfootball", "americanfootball", "football"} and "soccer" not in (game.league or "").lower():
        if market_type == SportsMarketType.MONEYLINE:
            return _nfl_ml_lock(side, game)
        if market_type == SportsMarketType.TOTALS and line is not None:
            return _nfl_totals_lock(side, line, game)
        if market_type == SportsMarketType.SPREADS and line is not None:
            return _nfl_spreads_lock(side, line, game)

    return _UNSUPPORTED


__all__ = [
    "MathLockResult",
    "evaluate_math_lock",
]
