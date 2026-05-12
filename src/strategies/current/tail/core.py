"""体育扫尾评估的通用核心：accept/reject 工具、候选构造、与体育无关的通用评估器。

凡是不依赖具体体育（generic Totals/Moneyline/Spreads）的逻辑都集中在这里；
MLB、Tennis 等体育专属评估器调用本模块返回结果。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from datetime import datetime

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
    return SportsTailCandidate(
        game=game,
        market=market,
        reason="tail_candidate",
        metadata={
            "market_family": market.market_family.value,
            "market_type": market.market_type.value,
            "side": market.side.value,
            "line": str(market.line) if market.line is not None else None,
            "best_ask": str(market.best_ask) if market.best_ask is not None else None,
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
    return TailEvaluation(
        accepted=False,
        action=TailAction.REJECT,
        reason=reason,
        candidate=candidate,
        execution_permission=None,
        metadata=dict(metadata or {}) if candidate is None else candidate.metadata,
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
    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    if market.line is None:
        return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
    if Decimal(candidate.game.score_diff_for(market.side)) + market.line > Decimal("0"):
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
