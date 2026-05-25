"""体育扫尾评估的通用核心：accept/reject 工具、候选构造、与体育无关的通用评估器。

凡是不依赖具体体育（generic Totals/Moneyline/Spreads）的逻辑都集中在这里；
MLB、Tennis 等体育专属评估器调用本模块返回结果。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from datetime import datetime

from strategies.current._shared.edge_gates import implied_mid_probability
from strategies.sports_framework import (
    LiveGameState,
    SportsMarketFamily,
    SportsMarketSide,
    SportsMarketSnapshot,
    SportsMarketType,
    market_scope,
)

from .types import (
    ExecutionPermission,
    SportsTailCandidate,
    SportsTailOpportunityType,
    TailAction,
    TailEvaluation,
    TailPolicy,
    TailRejectReason,
)


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _candidate(game: LiveGameState, market: SportsMarketSnapshot) -> SportsTailCandidate:
    implied = implied_mid_probability(market.best_bid, market.best_ask)
    return SportsTailCandidate(
        game=game,
        market=market,
        reason="tail_candidate",
        metadata={
            "market_family": market.market_family.value,
            "market_type": market.market_type.value,
            "side": market.side.value,
            "line": str(market.line) if market.line is not None else None,
            "best_bid": str(market.best_bid) if market.best_bid is not None else None,
            "best_ask": str(market.best_ask) if market.best_ask is not None else None,
            "implied_mid_prob": str(implied) if implied is not None else None,
            "scope_type": market_scope(market).scope_type.value,
            "scope_number": market_scope(market).scope_number,
            "market_end_date": _datetime_text(market.market_end_date),
            "total_score": game.total_score,
            "seconds_remaining": game.seconds_remaining,
            "game_status": game.status.value,
            "baseball_state": None if game.baseball_state is None else {
                "current_inning": game.baseball_state.current_inning,
                "inning_half": game.baseball_state.inning_half,
                "outs": game.baseball_state.outs,
                "offense_team": game.baseball_state.offense_team,
                "defense_team": game.baseball_state.defense_team,
                "occupied_bases": game.baseball_state.occupied_bases,
                "home_inning_runs": list(game.baseball_state.home_inning_runs),
                "away_inning_runs": list(game.baseball_state.away_inning_runs),
            },
            "tennis_state": None if game.tennis_state is None else {
                "home_sets_won": game.tennis_state.home_sets_won,
                "away_sets_won": game.tennis_state.away_sets_won,
                "current_set": game.tennis_state.current_set,
                "home_current_set_games": game.tennis_state.home_current_set_games,
                "away_current_set_games": game.tennis_state.away_current_set_games,
                "home_total_games": game.tennis_state.home_total_games,
                "away_total_games": game.tennis_state.away_total_games,
                "total_games": game.tennis_state.total_games,
                "set_scores": game.tennis_state.set_scores,
                "home_point": game.tennis_state.home_point,
                "away_point": game.tennis_state.away_point,
                "first_to_serve": game.tennis_state.first_to_serve,
                "serving_side": game.tennis_state.serving_side,
            },
        },
    )


def _accept(
    candidate: SportsTailCandidate,
    reason: str,
    permission: ExecutionPermission,
    *,
    opportunity_type: SportsTailOpportunityType = SportsTailOpportunityType.LIVE_TAIL,
) -> TailEvaluation:
    return TailEvaluation(
        accepted=True,
        action=_action_for_permission(permission),
        reason=reason,
        candidate=candidate,
        execution_permission=permission,
        opportunity_type=opportunity_type,
        metadata=candidate.metadata,
    )


def _reject(
    candidate: SportsTailCandidate | None,
    reason: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> TailEvaluation:
    # candidate.metadata 为基础,显式 metadata 覆盖——拒绝原因相关的数值证据
    # (如 edge / true_p / best_ask)必须能写入审计 metadata,供
    # CLAUDE.md §17 门限复盘使用。此前 candidate 非空时显式 metadata 被丢弃。
    base = dict(candidate.metadata) if candidate is not None else {}
    if metadata:
        base.update(metadata)
    return TailEvaluation(
        accepted=False,
        action=TailAction.REJECT,
        reason=reason,
        candidate=candidate,
        execution_permission=None,
        metadata=base,
    )


def _action_for_permission(permission: ExecutionPermission) -> TailAction:
    if permission == ExecutionPermission.RECORD_ONLY:
        return TailAction.RECORD
    if permission == ExecutionPermission.ALERT_ONLY:
        return TailAction.ALERT
    if permission == ExecutionPermission.MANUAL_CONFIRM:
        return TailAction.MANUAL_CONFIRM
    return TailAction.AUTO_EXECUTE


def _market_family_reject_reason(market_family: SportsMarketFamily) -> TailRejectReason | None:
    # series family 由 ``strategy._decide_series_entry`` → ``series.evaluator`` 处理，
    # 不会再到达 tail 评估器；防御性归到 UNSUPPORTED_MARKET_FAMILY，避免静默放行。
    if market_family == SportsMarketFamily.SINGLE_GAME:
        return None
    if market_family == SportsMarketFamily.OUTRIGHT:
        return TailRejectReason.OUTRIGHT_MARKET_NOT_AUTO_TRADABLE
    if market_family == SportsMarketFamily.ESPORTS:
        return TailRejectReason.ESPORTS_MARKET_NOT_AUTO_TRADABLE
    return TailRejectReason.UNSUPPORTED_MARKET_FAMILY


# ---- 通用 generic 评估器（非体育专属） --------------------------------


def _evaluate_totals(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    game = candidate.game
    market = candidate.market
    if market.line is None:
        return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)

    total_score = Decimal(game.total_score)
    if market.side == SportsMarketSide.OVER and total_score > market.line:
        return _accept(candidate, "totals_over_locked", policy.totals_execution_permission)

    if market.side == SportsMarketSide.UNDER:
        if game.seconds_remaining is None:
            return _reject(candidate, TailRejectReason.MISSING_SECONDS_REMAINING.value)
        if (
            game.seconds_remaining <= policy.max_under_seconds_remaining
            and market.line - total_score >= policy.min_under_safety_margin
        ):
            return _accept(candidate, "totals_under_near_locked", policy.totals_execution_permission)

    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


def _evaluate_moneyline(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    if game.seconds_remaining is None:
        return _reject(candidate, TailRejectReason.MISSING_SECONDS_REMAINING.value)
    if game.seconds_remaining > policy.max_moneyline_seconds_remaining:
        return _reject(candidate, TailRejectReason.GAME_NOT_LATE_ENOUGH.value)
    if game.score_diff_for(market.side) < policy.min_moneyline_lead:
        return _reject(candidate, TailRejectReason.INSUFFICIENT_LEAD.value)
    return _accept(candidate, "moneyline_late_lead", policy.moneyline_execution_permission)


def _evaluate_spreads(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    if market.line is None:
        return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
    if game.seconds_remaining is None:
        return _reject(candidate, TailRejectReason.MISSING_SECONDS_REMAINING.value)
    if game.seconds_remaining > policy.max_spreads_seconds_remaining:
        return _reject(candidate, TailRejectReason.GAME_NOT_LATE_ENOUGH.value)

    safety_margin = Decimal(game.score_diff_for(market.side)) + market.line
    if safety_margin < policy.min_spread_safety_margin:
        return _reject(candidate, TailRejectReason.INSUFFICIENT_SAFETY_MARGIN.value)
    return _accept(candidate, "spreads_late_cover", policy.spreads_execution_permission)


def _evaluate_basketball_first_half(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """评估篮球上半场盘口（1H total / spread / moneyline）。

    上半场结束（进入第 3 节或之后）后，1H 结果由第 1、2 节得分 100% 决定，
    是干净的锁定扫尾。ask/价格/流动性门禁已在通用 _common_reject_reason 处理。
    """
    game = candidate.game
    market = candidate.market
    state = game.basketball_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_BASKETBALL_STATE.value)
    if (state.current_period or 0) < 3:
        return _reject(candidate, TailRejectReason.BASKETBALL_FIRST_HALF_NOT_COMPLETE.value)
    home_q = state.home_quarter_scores
    away_q = state.away_quarter_scores
    if len(home_q) < 2 or len(away_q) < 2:
        return _reject(candidate, TailRejectReason.MISSING_BASKETBALL_STATE.value)
    if None in (home_q[0], home_q[1], away_q[0], away_q[1]):
        return _reject(candidate, TailRejectReason.MISSING_BASKETBALL_STATE.value)
    home_1h = home_q[0] + home_q[1]
    away_1h = away_q[0] + away_q[1]

    if market.market_type == SportsMarketType.TOTALS:
        if market.line is None:
            return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
        total_1h = Decimal(home_1h + away_1h)
        if market.side == SportsMarketSide.OVER and total_1h > market.line:
            return _accept(candidate, "basketball_1h_total_over_locked", policy.totals_execution_permission)
        if market.side == SportsMarketSide.UNDER and total_1h < market.line:
            return _accept(candidate, "basketball_1h_total_under_locked", policy.totals_execution_permission)
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)

    if market.market_type == SportsMarketType.MONEYLINE:
        if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
            return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
        margin = home_1h - away_1h if market.side == SportsMarketSide.HOME else away_1h - home_1h
        if margin > 0:
            return _accept(candidate, "basketball_1h_moneyline_locked", policy.moneyline_execution_permission)
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)

    if market.market_type == SportsMarketType.SPREADS:
        if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY} or market.line is None:
            return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
        margin = home_1h - away_1h if market.side == SportsMarketSide.HOME else away_1h - home_1h
        if Decimal(margin) + market.line > 0:
            return _accept(candidate, "basketball_1h_spread_locked", policy.spreads_execution_permission)
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)

    return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)


def _basketball_segment_scores(
    state, lo_index: int, hi_index: int
) -> tuple[int, int] | None:
    """累加 [lo_index, hi_index] 区间内各节得分；任一节缺失则返回 None。

    下标 0 = 第 1 节。区间内任意一节为 None 表示该分段尚未打完或数据缺失，
    不能据此锁定分段结果。
    """

    home_q = state.home_quarter_scores
    away_q = state.away_quarter_scores
    if len(home_q) <= hi_index or len(away_q) <= hi_index:
        return None
    home_total = 0
    away_total = 0
    for idx in range(lo_index, hi_index + 1):
        if home_q[idx] is None or away_q[idx] is None:
            return None
        home_total += home_q[idx]
        away_total += away_q[idx]
    return home_total, away_total


def _basketball_segment_evaluation(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
    home_segment: int,
    away_segment: int,
    reason_prefix: str,
) -> TailEvaluation:
    """用已锁定的分段比分判定 ML / spread——分段完成后结果 100% 确定。"""

    market = candidate.market
    if market.market_type == SportsMarketType.MONEYLINE:
        if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
            return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
        margin = (
            home_segment - away_segment
            if market.side == SportsMarketSide.HOME
            else away_segment - home_segment
        )
        if margin > 0:
            return _accept(candidate, f"{reason_prefix}_moneyline_locked", policy.moneyline_execution_permission)
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)

    if market.market_type == SportsMarketType.SPREADS:
        if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY} or market.line is None:
            return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
        margin = (
            home_segment - away_segment
            if market.side == SportsMarketSide.HOME
            else away_segment - home_segment
        )
        if Decimal(margin) + market.line > 0:
            return _accept(candidate, f"{reason_prefix}_spread_locked", policy.spreads_execution_permission)
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)

    return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)


def _evaluate_basketball_quarter(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """评估篮球单节盘口（Q1-Q4 的 moneyline / spread）。

    某节结果由该节双方得分 100% 决定；该节必须已结束（current_period 已
    推进到下一节或更后）才视为锁定。节比分由 BasketballGameState 的
    home_quarter_scores / away_quarter_scores 给出，与 1H 评估同源。
    """

    game = candidate.game
    market = candidate.market
    state = game.basketball_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_BASKETBALL_STATE.value)
    scope = market_scope(market)
    quarter = scope.scope_number
    if quarter is None or quarter < 1 or quarter > 4:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SCOPE.value)
    # 该节必须已打完：当前节已推进到 quarter 之后。
    if (state.current_period or 0) <= quarter:
        return _reject(candidate, TailRejectReason.BASKETBALL_QUARTER_NOT_COMPLETE.value)
    segment = _basketball_segment_scores(state, quarter - 1, quarter - 1)
    if segment is None:
        return _reject(candidate, TailRejectReason.MISSING_BASKETBALL_STATE.value)
    return _basketball_segment_evaluation(
        candidate, policy, segment[0], segment[1], f"basketball_q{quarter}"
    )


def _evaluate_basketball_second_half(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """评估篮球下半场盘口（2H = Q3+Q4 的 moneyline / spread）。

    下半场结果由第 3、4 节得分 100% 决定，正赛打完（进入加时 current_period
    >= 5）后即锁定。常规情况下半场打完意味着整场已结束，会走 ENDED 路径；
    此处覆盖加时场景下 2H 已定但比赛仍 live 的情形。
    """

    state = candidate.game.basketball_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_BASKETBALL_STATE.value)
    if (state.current_period or 0) <= 4:
        return _reject(candidate, TailRejectReason.BASKETBALL_SECOND_HALF_NOT_COMPLETE.value)
    segment = _basketball_segment_scores(state, 2, 3)
    if segment is None:
        return _reject(candidate, TailRejectReason.MISSING_BASKETBALL_STATE.value)
    return _basketball_segment_evaluation(
        candidate, policy, segment[0], segment[1], "basketball_2h"
    )


def _soccer_halftime_result_direction(market: SportsMarketSnapshot) -> str | None:
    """从 slug 提取半场赛果方向：home / draw / away。"""
    slug = (market.market_slug or "").lower()
    for direction in ("home", "draw", "away"):
        if slug.endswith(f"halftime-result-{direction}"):
            return direction
    return None


def is_soccer_halftime_market(market: SportsMarketSnapshot) -> bool:
    """识别足球半场赛果盘口（halftime-result-home/draw/away）。"""
    return _soccer_halftime_result_direction(market) is not None


def _evaluate_soccer_halftime_result(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """评估足球半场赛果盘口（halftime-result）。

    数据源在半场结束后才给出 <ht> 比分；两侧半场比分齐全即表示半场已锁定，
    赛果 100% 确定。binary_prop 在通用门禁跳过 ask 检查，这里自查入场价。
    """
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    direction = _soccer_halftime_result_direction(market)
    if direction is None:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SCOPE.value)
    state = game.soccer_state
    if state is None or state.home_halftime_score is None or state.away_halftime_score is None:
        return _reject(candidate, TailRejectReason.SOCCER_HALFTIME_NOT_COMPLETE.value)
    # Parser bug 守卫: goalserve 数据源可能在 1st_half 中提供 home/away_halftime_score
    # 数值 (而非 None),导致 1st_half 第 6 分钟就被误判"半场 0-0 已锁定 Draw"。
    # 实盘案例: THE-SAG 1st Half 第 6min 时被误判 Draw 锁定,买入后 Thespa 进球 → 巨亏。
    #
    # 半场结果真锁定 = period 进入 second_half/ended/full_time;或 1st_half clock >= 45
    # (进入伤停,半场即将结束,数学锁定)。前两者由 parser 显式区分,后者放宽允许末段进场。
    period = (state.period or "").lower()
    if period in {"second_half", "ended", "full_time"}:
        pass  # 半场已结束,halftime_score 真值
    elif period == "first_half" and (state.clock_minutes or 0) >= 45:
        pass  # 1st half 进入伤停时间,接近半场结束,接受数学锁定场景
    else:
        return _reject(candidate, TailRejectReason.SOCCER_HALFTIME_NOT_COMPLETE.value)
    if market.best_ask is None:
        return _reject(candidate, TailRejectReason.MISSING_BEST_ASK.value)
    # price/liquidity 入场 gate 已删——宽进严管，持仓策略接管止盈止损。

    h = state.home_halftime_score
    a = state.away_halftime_score
    actual = "home" if h > a else ("away" if a > h else "draw")
    won = actual == direction
    if market.side == SportsMarketSide.YES:
        if won:
            return _accept(candidate, "soccer_halftime_result_locked", policy.totals_execution_permission)
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)
    # side NO：赛果已确定且不是该方向 → No 锁定。
    if not won:
        return _accept(candidate, "soccer_halftime_result_locked", policy.totals_execution_permission)
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


def is_soccer_btts_market(market: SportsMarketSnapshot) -> bool:
    """识别足球 BTTS（both teams to score，双方进球）盘口。"""
    slug = (market.market_slug or "").lower()
    return slug.endswith("-btts") or slug.endswith("-both-teams-to-score")


def _evaluate_soccer_btts(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """评估足球 BTTS（双方进球）盘口。

    双方均已进球 → BTTS YES 100% 锁定：进球既成事实、不可撤销，无论比赛
    后续如何 YES 必中。NO 侧在比赛结束前无法锁定（0 球方随时可能进球），
    本盘中评估器只对 YES 给出锁定。binary_prop 在通用门禁跳过 ask 检查，
    这里自查入场价。
    """
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    if market.best_ask is None:
        return _reject(candidate, TailRejectReason.MISSING_BEST_ASK.value)
    # price/liquidity 入场 gate 已删——宽进严管，持仓策略接管止盈止损。

    both_scored = game.home_score >= 1 and game.away_score >= 1
    if market.side == SportsMarketSide.YES:
        if both_scored:
            return _accept(candidate, "soccer_btts_yes_locked", policy.totals_execution_permission)
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)
    # NO：比赛结束前 0 球方仍可能进球——盘中不可锁定。
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


# 足球胜负锁定所需净胜球（按剩余分钟缩放）。进球稀少——临近终场对手越难
# 追回，所需领先越小。
_SOCCER_MONEYLINE_MARGIN_BY_MINUTES_LEFT: tuple[tuple[float, int], ...] = (
    (5.0, 2),    # ≤5 分钟：净胜 ≥2
    (15.0, 3),   # ≤15 分钟：净胜 ≥3
)


def _soccer_moneyline_direction(market: SportsMarketSnapshot) -> str | None:
    """足球 3-way 胜平负 binary 盘的方向。

    slug 形如 ``{league}-{home}-{away}-{yyyy}-{mm}-{dd}-{suffix}``（恰 7 段）：
    suffix == 主队缩写 → home；== 客队缩写 → away；== ``draw`` → draw。
    段数不为 7 的盘口（halftime/total/handicap 等）不在此识别。
    """
    slug = (market.market_slug or "").strip().lower()
    parts = [p for p in slug.split("-") if p]
    if len(parts) != 7:
        return None
    home_abbr, away_abbr, suffix = parts[1], parts[2], parts[-1]
    if suffix == "draw":
        return "draw"
    if suffix == home_abbr:
        return "home"
    if suffix == away_abbr:
        return "away"
    return None


def is_soccer_moneyline_market(market: SportsMarketSnapshot) -> bool:
    """识别足球胜负盘（3-way 拆成的 binary_prop）。"""
    return _soccer_moneyline_direction(market) is not None


def _soccer_moneyline_required_margin(seconds_remaining: int) -> int | None:
    """按剩余时间返回足球胜负锁定所需净胜球；超过 15 分钟（太早）返回 None。"""
    minutes_left = seconds_remaining / 60.0
    for cutoff, margin in _SOCCER_MONEYLINE_MARGIN_BY_MINUTES_LEFT:
        if minutes_left <= cutoff:
            return margin
    return None


def _evaluate_soccer_moneyline(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """足球胜负盘评估：进球稀少，临近终场领先达到安全净胜球时锁定。

    平局盘中无可靠锁定模型——给可审计原因后跳过。binary_prop 在通用门禁
    跳过 ask 检查，这里自查入场价。
    """
    game = candidate.game
    market = candidate.market
    direction = _soccer_moneyline_direction(market)
    if direction is None:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SCOPE.value)
    if direction == "draw":
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)
    if market.side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    if market.best_ask is None:
        return _reject(candidate, TailRejectReason.MISSING_BEST_ASK.value)
    # price/liquidity 入场 gate 已删——宽进严管，持仓策略接管止盈止损。
    if game.seconds_remaining is None:
        return _reject(candidate, TailRejectReason.MISSING_SECONDS_REMAINING.value)
    required = _soccer_moneyline_required_margin(game.seconds_remaining)
    if required is None:
        return _reject(candidate, TailRejectReason.GAME_NOT_LATE_ENOUGH.value)
    if direction == "home":
        lead = game.home_score - game.away_score
    else:
        lead = game.away_score - game.home_score
    if market.side == SportsMarketSide.YES:
        if lead >= required:
            return _accept(candidate, "soccer_moneyline_locked", policy.moneyline_execution_permission)
        return _reject(candidate, TailRejectReason.INSUFFICIENT_LEAD.value)
    # NO：该方向落后达到安全净胜球 → 已不可能赢 → NO 锁定。
    if -lead >= required:
        return _accept(candidate, "soccer_moneyline_no_locked", policy.moneyline_execution_permission)
    return _reject(candidate, TailRejectReason.INSUFFICIENT_LEAD.value)


def _soccer_exact_score_target(market: SportsMarketSnapshot) -> tuple[int, int] | None:
    """从 slug 解析精确比分目标 (home, away)。slug 形如 ...-exact-score-{h}-{a}。"""
    slug = (market.market_slug or "").lower()
    if "exact-score-" not in slug:
        return None
    parts = slug.split("-")
    if len(parts) < 2:
        return None
    try:
        return int(parts[-2]), int(parts[-1])
    except ValueError:
        return None


def is_soccer_exact_score_market(market: SportsMarketSnapshot) -> bool:
    """识别足球精确比分盘口（exact-score-{h}-{a}）。"""
    return _soccer_exact_score_target(market) is not None


def _evaluate_soccer_exact_score(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """足球精确比分盘评估。

    比分只增不减：当前比分一旦在任一方向超过目标 → 该精确比分永不可能成立
    → NO 100% 锁定。YES 仅在终场比分恰好等于目标时成立，盘中不可锁定（比分
    仍可能继续变化）。binary_prop 在通用门禁跳过 ask 检查，这里自查入场价。
    """
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    target = _soccer_exact_score_target(market)
    if target is None:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SCOPE.value)
    if market.best_ask is None:
        return _reject(candidate, TailRejectReason.MISSING_BEST_ASK.value)
    # price/liquidity 入场 gate 已删——宽进严管，持仓策略接管止盈止损。

    target_home, target_away = target
    # 任一方现有比分已超过目标 → 终场永不可能恰为该精确比分。
    score_passed_target = game.home_score > target_home or game.away_score > target_away
    if market.side == SportsMarketSide.NO:
        if score_passed_target:
            return _accept(candidate, "soccer_exact_score_no_locked", policy.totals_execution_permission)
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)
    # YES：终场前比分仍可变化——盘中不可锁定。
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


# ---- ended-not-closed 通用评估器 --------------------------------------


def _evaluate_ended_totals(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    market = candidate.market
    if market.line is None:
        return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
    total_score = Decimal(candidate.game.total_score)
    if market.side == SportsMarketSide.OVER and total_score > market.line:
        return _accept(
            candidate,
            "ended_not_closed_totals_over",
            policy.totals_execution_permission,
            opportunity_type=SportsTailOpportunityType.ENDED_NOT_CLOSED,
        )
    if market.side == SportsMarketSide.UNDER and total_score < market.line:
        return _accept(
            candidate,
            "ended_not_closed_totals_under",
            policy.totals_execution_permission,
            opportunity_type=SportsTailOpportunityType.ENDED_NOT_CLOSED,
        )
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


def _evaluate_ended_moneyline(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    if candidate.game.score_diff_for(market.side) > 0:
        return _accept(
            candidate,
            "ended_not_closed_moneyline",
            policy.moneyline_execution_permission,
            opportunity_type=SportsTailOpportunityType.ENDED_NOT_CLOSED,
        )
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


def _evaluate_ended_spreads(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """ended spread:Final 比分锁定后才考虑买 spread。

    Bug 历史: 旧公式 `score_diff + line > 0` 等价于 "home +line(underdog)cover",
    但 polymarket spread market question 通常是 "Team (-line)" favorite — line 字段
    是绝对值,真方向是 side 需要赢 +line+ 才 cover。实盘验证:
    Polymarket "Spread: Hokkaidō Consadole Sapporo (-2.5)" Final 0-1(home 输1) →
    旧公式 -1 + 2.5 = 1.5 > 0 错 accept,实际 home 没 cover -2.5 直接锁定输。

    正确公式: side 视角 score_diff >= line+1 才严格 cover (即 score_diff > line)。
    Final 0-1, line=2.5: score_diff(home)=-1, -1 > 2.5 = False → reject ✓
    Final 3-0, line=2.5: score_diff(home)=3, 3 > 2.5 = True → accept ✓
    """

    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    if market.line is None:
        return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
    score_diff = Decimal(candidate.game.score_diff_for(market.side))
    if score_diff > market.line:
        return _accept(
            candidate,
            "ended_not_closed_spreads",
            policy.spreads_execution_permission,
            opportunity_type=SportsTailOpportunityType.ENDED_NOT_CLOSED,
        )
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


# ---- scale-in 通用评估器 ----------------------------------------------


def _evaluate_moneyline_scale_in(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    if game.seconds_remaining is None:
        return _reject(candidate, TailRejectReason.MISSING_SECONDS_REMAINING.value)
    if game.seconds_remaining > policy.max_moneyline_seconds_remaining // 2:
        return _reject(candidate, TailRejectReason.GAME_NOT_LATE_ENOUGH.value)
    if game.score_diff_for(market.side) < policy.min_moneyline_lead + 2:
        return _reject(candidate, TailRejectReason.INSUFFICIENT_LEAD.value)
    return _accept(
        candidate,
        "scale_in_moneyline_advantage",
        policy.moneyline_execution_permission,
        opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
    )


def _evaluate_spreads_scale_in(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    if market.line is None:
        return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
    if game.seconds_remaining is None:
        return _reject(candidate, TailRejectReason.MISSING_SECONDS_REMAINING.value)
    if game.seconds_remaining > policy.max_spreads_seconds_remaining // 2:
        return _reject(candidate, TailRejectReason.GAME_NOT_LATE_ENOUGH.value)
    safety_margin = Decimal(game.score_diff_for(market.side)) + market.line
    if safety_margin < policy.min_spread_safety_margin + Decimal("1"):
        return _reject(candidate, TailRejectReason.INSUFFICIENT_SAFETY_MARGIN.value)
    return _accept(
        candidate,
        "scale_in_spreads_advantage",
        policy.spreads_execution_permission,
        opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
    )


def _evaluate_totals_scale_in(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    game = candidate.game
    market = candidate.market
    if market.line is None:
        return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
    total_score = Decimal(game.total_score)
    if market.side == SportsMarketSide.OVER and total_score - market.line >= Decimal("1"):
        return _accept(
            candidate,
            "scale_in_totals_over_advantage",
            policy.totals_execution_permission,
            opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
        )
    if market.side == SportsMarketSide.UNDER:
        if game.seconds_remaining is None:
            return _reject(candidate, TailRejectReason.MISSING_SECONDS_REMAINING.value)
        if game.seconds_remaining > policy.max_under_seconds_remaining // 2:
            return _reject(candidate, TailRejectReason.GAME_NOT_LATE_ENOUGH.value)
        if market.line - total_score < policy.min_under_safety_margin + Decimal("1"):
            return _reject(candidate, TailRejectReason.INSUFFICIENT_SAFETY_MARGIN.value)
        return _accept(
            candidate,
            "scale_in_totals_under_advantage",
            policy.totals_execution_permission,
            opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
        )
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


# ---- 通用 tail-state-reached（用于 endDate 粗筛绕过） ------------------


def _standard_totals_tail_state_reached(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: TailPolicy,
) -> bool:
    if market.line is None:
        return False
    total_score = Decimal(game.total_score)
    if market.side == SportsMarketSide.OVER:
        return total_score > market.line
    if market.side != SportsMarketSide.UNDER or game.seconds_remaining is None:
        return False
    return (
        game.seconds_remaining <= policy.max_under_seconds_remaining
        and market.line - total_score >= policy.min_under_safety_margin
    )


def _standard_moneyline_tail_state_reached(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: TailPolicy,
) -> bool:
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY} or game.seconds_remaining is None:
        return False
    return (
        game.seconds_remaining <= policy.max_moneyline_seconds_remaining
        and game.score_diff_for(market.side) >= policy.min_moneyline_lead
    )


def _standard_spreads_tail_state_reached(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: TailPolicy,
) -> bool:
    if (
        market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}
        or market.line is None
        or game.seconds_remaining is None
    ):
        return False
    safety_margin = Decimal(game.score_diff_for(market.side)) + market.line
    return (
        game.seconds_remaining <= policy.max_spreads_seconds_remaining
        and safety_margin >= policy.min_spread_safety_margin
    )


def _standard_tail_state_reached(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: TailPolicy,
) -> bool:
    """复用常规运动尾盘条件判断是否可忽略 Gamma 的远期 endDate。"""

    if market.market_type == SportsMarketType.TOTALS:
        return _standard_totals_tail_state_reached(game, market, policy)
    if market.market_type == SportsMarketType.MONEYLINE:
        return _standard_moneyline_tail_state_reached(game, market, policy)
    if market.market_type == SportsMarketType.SPREADS:
        return _standard_spreads_tail_state_reached(game, market, policy)
    return False
