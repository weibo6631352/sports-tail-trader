"""网球（ATP / WTA）专属评估器、状态工具与 tail-state-reached。

包含 live 评估、ended-not-closed 评估、scale-in 评估三类入口，
以及围绕 ``TennisGameState`` 的私有锁定/拐点判定。
"""

from __future__ import annotations

from decimal import Decimal

from strategies.sports_framework import (
    SportsMarketScopeType,
    SportsMarketSide,
    SportsMarketSnapshot,
    SportsMarketType,
    TennisGameState,
)

from .core import _accept, _reject
from .slug import (
    _is_tennis_set_winner_market,
    _tennis_set_winner_number,
    _tennis_total_scope,
)
from .types import (
    SportsTailCandidate,
    SportsTailOpportunityType,
    TailEvaluation,
    TailPolicy,
    TailRejectReason,
)


# ---- live 评估 -------------------------------------------------------


def _evaluate_tennis_totals(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """评估网球 totals 盘口。

    网球 totals 至少分为整场总局数和总盘数两类。这里先按 market slug 区分
    结算对象，避免把 ``set-totals-2.5`` 错当成整场总局数。
    """

    market = candidate.market
    state = candidate.game.tennis_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_TENNIS_STATE.value)
    if market.line is None:
        return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
    scope = _tennis_total_scope(market)
    if market.side == SportsMarketSide.UNDER:
        if scope.scope_type == SportsMarketScopeType.TENNIS_SET_GAMES and _tennis_set_games_total_is_under_locked(
            state,
            scope.scope_number,
            market.line,
        ):
            return _accept(candidate, "tennis_set_games_under_locked", policy.totals_execution_permission)
        return _reject(candidate, TailRejectReason.TENNIS_TOTALS_UNDER_NOT_SUPPORTED.value)
    if market.side != SportsMarketSide.OVER:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    if scope.scope_type == SportsMarketScopeType.TENNIS_MATCH_GAMES and Decimal(state.total_games) > market.line:
        return _accept(candidate, "tennis_totals_over_locked", policy.totals_execution_permission)
    if scope.scope_type == SportsMarketScopeType.TENNIS_MATCH_GAMES and _tennis_match_total_min_final_games_is_over(state, market.line):
        return _accept(
            candidate,
            "tennis_totals_over_min_final_games_locked",
            policy.totals_execution_permission,
        )
    if scope.scope_type == SportsMarketScopeType.TENNIS_TOTAL_SETS and _tennis_sets_total_is_over(state, market.line):
        return _accept(candidate, "tennis_set_totals_over_locked", policy.totals_execution_permission)
    if scope.scope_type == SportsMarketScopeType.TENNIS_SET_GAMES and _tennis_set_games_total_is_over(
        state,
        scope.scope_number,
        market.line,
    ):
        return _accept(candidate, "tennis_set_games_over_locked", policy.totals_execution_permission)
    if scope.scope_type == SportsMarketScopeType.UNSUPPORTED_PERIOD:
        return _reject(candidate, TailRejectReason.TENNIS_TOTAL_SCOPE_UNSUPPORTED.value)
    return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)


def _evaluate_tennis_moneyline(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """评估网球胜负线的临近锁定场景。

    网球没有固定倒计时，因此只使用“已领先盘数 + 当前盘接近拿下”的结构化状态。
    这比按页面价格或普通比分硬推更保守，也便于后续用回放校准阈值。
    """

    market = candidate.market
    state = candidate.game.tennis_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_TENNIS_STATE.value)
    if _is_tennis_set_winner_market(market):
        return _evaluate_tennis_set_winner(candidate, policy)
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    side_games = state.current_set_games_for(market.side)
    other_side = SportsMarketSide.AWAY if market.side == SportsMarketSide.HOME else SportsMarketSide.HOME
    other_games = state.current_set_games_for(other_side)
    if side_games is None or other_games is None:
        return _reject(candidate, TailRejectReason.MISSING_TENNIS_STATE.value)
    game_lead = side_games - other_games
    set_lead = state.sets_won_for(market.side) - state.sets_won_for(other_side)
    if side_games >= 5 and game_lead >= 2 and set_lead >= 1:
        return _accept(candidate, "tennis_moneyline_near_locked", policy.moneyline_execution_permission)
    return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)


def _tennis_set_is_complete(home_games: int, away_games: int) -> bool:
    """判断一盘局分是否已打完。

    标准盘：先到 6 局且净胜 ≥2（6-0..6-4、7-5、8-6 等）；或抢七 7-6。
    未打完的局分（如 5-2、6-5）不算完成——set winner 不能据此判定锁定。
    """
    hi = max(home_games, away_games)
    lo = min(home_games, away_games)
    if hi >= 6 and hi - lo >= 2:
        return True
    if hi >= 7 and hi - lo == 1:
        return True
    return False


def _evaluate_tennis_set_winner(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """用 SofaScore 每盘局分判断 set winner 盘口是否已经锁定。"""

    market = candidate.market
    state = candidate.game.tennis_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_TENNIS_STATE.value)
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    set_number = _tennis_set_winner_number(market)
    if set_number is None or not state.set_scores:
        return _reject(candidate, TailRejectReason.TENNIS_SET_WINNER_NOT_SUPPORTED.value)
    if state.current_set == set_number and _tennis_current_set_side_near_locked(state, market.side):
        return _accept(
            candidate,
            "tennis_set_winner_current_set_near_locked",
            policy.moneyline_execution_permission,
        )
    if state.current_set is not None and state.current_set <= set_number:
        return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)
    if len(state.set_scores) < set_number:
        return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)

    home_games, away_games = state.set_scores[set_number - 1]
    # 必须确认该盘真的打完——set_scores 里可能是进行中的局分（如 5-2），
    # 据此判定 set winner 锁定会在未决出的盘上误下单。
    if not _tennis_set_is_complete(home_games, away_games):
        return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)
    side_games = home_games if market.side == SportsMarketSide.HOME else away_games
    other_games = away_games if market.side == SportsMarketSide.HOME else home_games
    if side_games > other_games:
        return _accept(candidate, "tennis_set_winner_locked", policy.moneyline_execution_permission)
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


# ---- ended-not-closed 评估 -------------------------------------------


def _evaluate_ended_tennis(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    market = candidate.market
    state = candidate.game.tennis_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_TENNIS_STATE.value)
    if market.market_type == SportsMarketType.TOTALS:
        if market.line is None:
            return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
        scope = _tennis_total_scope(market)
        if scope.scope_type == SportsMarketScopeType.UNSUPPORTED_PERIOD:
            return _reject(candidate, TailRejectReason.TENNIS_TOTAL_SCOPE_UNSUPPORTED.value)
        if scope.scope_type == SportsMarketScopeType.TENNIS_TOTAL_SETS:
            completed = Decimal(state.home_sets_won + state.away_sets_won)
            if market.side == SportsMarketSide.OVER and completed > market.line:
                return _accept(
                    candidate,
                    "ended_not_closed_tennis_sets_over",
                    policy.totals_execution_permission,
                    opportunity_type=SportsTailOpportunityType.ENDED_NOT_CLOSED,
                )
            if market.side == SportsMarketSide.UNDER and completed < market.line:
                return _accept(
                    candidate,
                    "ended_not_closed_tennis_sets_under",
                    policy.totals_execution_permission,
                    opportunity_type=SportsTailOpportunityType.ENDED_NOT_CLOSED,
                )
        elif scope.scope_type == SportsMarketScopeType.TENNIS_MATCH_GAMES:
            total_games = Decimal(state.total_games)
            if market.side == SportsMarketSide.OVER and total_games > market.line:
                return _accept(
                    candidate,
                    "ended_not_closed_tennis_games_over",
                    policy.totals_execution_permission,
                    opportunity_type=SportsTailOpportunityType.ENDED_NOT_CLOSED,
                )
            if market.side == SportsMarketSide.UNDER and total_games < market.line:
                return _accept(
                    candidate,
                    "ended_not_closed_tennis_games_under",
                    policy.totals_execution_permission,
                    opportunity_type=SportsTailOpportunityType.ENDED_NOT_CLOSED,
                )
        elif scope.scope_type == SportsMarketScopeType.TENNIS_SET_GAMES:
            set_games = _tennis_set_games_total(state, scope.scope_number)
            if set_games is None:
                return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)
            total_games = Decimal(set_games)
            if market.side == SportsMarketSide.OVER and total_games > market.line:
                return _accept(
                    candidate,
                    "ended_not_closed_tennis_set_games_over",
                    policy.totals_execution_permission,
                    opportunity_type=SportsTailOpportunityType.ENDED_NOT_CLOSED,
                )
            if market.side == SportsMarketSide.UNDER and total_games < market.line:
                return _accept(
                    candidate,
                    "ended_not_closed_tennis_set_games_under",
                    policy.totals_execution_permission,
                    opportunity_type=SportsTailOpportunityType.ENDED_NOT_CLOSED,
                )
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)
    if market.market_type == SportsMarketType.MONEYLINE:
        if _is_tennis_set_winner_market(market):
            return _evaluate_ended_tennis_set_winner(candidate, policy)
        if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
            return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
        other_side = SportsMarketSide.AWAY if market.side == SportsMarketSide.HOME else SportsMarketSide.HOME
        if state.sets_won_for(market.side) > state.sets_won_for(other_side):
            return _accept(
                candidate,
                "ended_not_closed_tennis_moneyline",
                policy.moneyline_execution_permission,
                opportunity_type=SportsTailOpportunityType.ENDED_NOT_CLOSED,
            )
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)
    return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)


def _evaluate_ended_tennis_set_winner(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    market = candidate.market
    state = candidate.game.tennis_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_TENNIS_STATE.value)
    set_number = _tennis_set_winner_number(market)
    if set_number is None or len(state.set_scores) < set_number:
        return _reject(candidate, TailRejectReason.TENNIS_SET_WINNER_NOT_SUPPORTED.value)
    home_games, away_games = state.set_scores[set_number - 1]
    if not _tennis_set_is_complete(home_games, away_games):
        return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)
    side_games = home_games if market.side == SportsMarketSide.HOME else away_games
    other_games = away_games if market.side == SportsMarketSide.HOME else home_games
    if side_games > other_games:
        return _accept(
            candidate,
            "ended_not_closed_tennis_set_winner",
            policy.moneyline_execution_permission,
            opportunity_type=SportsTailOpportunityType.ENDED_NOT_CLOSED,
        )
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


# ---- scale-in 评估 ---------------------------------------------------


def _evaluate_tennis_scale_in(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    market = candidate.market
    if market.market_type == SportsMarketType.TOTALS:
        return _evaluate_tennis_totals_scale_in(candidate, policy)
    if market.market_type != SportsMarketType.MONEYLINE:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)
    state = candidate.game.tennis_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_TENNIS_STATE.value)
    if _is_tennis_set_winner_market(market):
        return _reject(candidate, TailRejectReason.TENNIS_SET_WINNER_NOT_SUPPORTED.value)
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    side_games = state.current_set_games_for(market.side)
    other_side = SportsMarketSide.AWAY if market.side == SportsMarketSide.HOME else SportsMarketSide.HOME
    other_games = state.current_set_games_for(other_side)
    if side_games is None or other_games is None:
        return _reject(candidate, TailRejectReason.MISSING_TENNIS_STATE.value)
    set_lead = state.sets_won_for(market.side) - state.sets_won_for(other_side)
    if side_games >= 5 and side_games - other_games >= 3 and set_lead >= 0:
        return _accept(
            candidate,
            "scale_in_tennis_moneyline_advantage",
            policy.moneyline_execution_permission,
            opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
        )
    return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)


def _evaluate_tennis_totals_scale_in(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """按网球盘口结算范围评估 totals 受控加仓。"""

    market = candidate.market
    state = candidate.game.tennis_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_TENNIS_STATE.value)
    if market.line is None:
        return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
    scope = _tennis_total_scope(market)
    if market.side == SportsMarketSide.UNDER:
        if scope.scope_type == SportsMarketScopeType.TENNIS_SET_GAMES and _tennis_set_games_total_is_under_locked(
            state,
            scope.scope_number,
            market.line,
        ):
            return _accept(
                candidate,
                "scale_in_tennis_set_games_under_advantage",
                policy.totals_execution_permission,
                opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
            )
        return _reject(candidate, TailRejectReason.TENNIS_TOTALS_UNDER_NOT_SUPPORTED.value)
    if market.side != SportsMarketSide.OVER:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)

    if scope.scope_type == SportsMarketScopeType.TENNIS_MATCH_GAMES:
        total_games = Decimal(state.total_games)
        if total_games - market.line >= Decimal("1") or _tennis_match_total_min_final_games_is_over(
            state,
            market.line,
        ):
            return _accept(
                candidate,
                "scale_in_tennis_totals_over_advantage",
                policy.totals_execution_permission,
                opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
            )
        return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)
    if scope.scope_type == SportsMarketScopeType.TENNIS_TOTAL_SETS:
        if _tennis_sets_total_is_over(state, market.line):
            return _accept(
                candidate,
                "scale_in_tennis_set_totals_over_advantage",
                policy.totals_execution_permission,
                opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
            )
        return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)
    if scope.scope_type == SportsMarketScopeType.TENNIS_SET_GAMES:
        if _tennis_set_games_total_is_over(state, scope.scope_number, market.line):
            return _accept(
                candidate,
                "scale_in_tennis_set_games_over_advantage",
                policy.totals_execution_permission,
                opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
            )
        return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)
    return _reject(candidate, TailRejectReason.TENNIS_TOTAL_SCOPE_UNSUPPORTED.value)


# ---- tail-state-reached（用于 endDate 粗筛绕过） ---------------------


def _tennis_tail_state_reached(game, market: SportsMarketSnapshot) -> bool:
    """判断网球是否已达到可前置订阅和入场的尾盘结构。"""

    state = game.tennis_state
    if state is None:
        return False
    if market.market_type == SportsMarketType.TOTALS:
        return _tennis_totals_tail_state_reached(state, market)
    if market.market_type != SportsMarketType.MONEYLINE:
        return False
    if _is_tennis_set_winner_market(market):
        return _tennis_set_winner_tail_state_reached(state, market)
    return _tennis_moneyline_tail_state_reached(state, market)


def _tennis_totals_tail_state_reached(state: TennisGameState, market: SportsMarketSnapshot) -> bool:
    if market.line is None:
        return False
    scope = _tennis_total_scope(market)
    if market.side == SportsMarketSide.UNDER:
        return (
            scope.scope_type == SportsMarketScopeType.TENNIS_SET_GAMES
            and _tennis_set_games_total_is_under_locked(state, scope.scope_number, market.line)
        )
    if market.side != SportsMarketSide.OVER:
        return False
    if scope.scope_type == SportsMarketScopeType.TENNIS_MATCH_GAMES:
        return Decimal(state.total_games) > market.line or _tennis_match_total_min_final_games_is_over(
            state,
            market.line,
        )
    if scope.scope_type == SportsMarketScopeType.TENNIS_TOTAL_SETS:
        return _tennis_sets_total_is_over(state, market.line)
    if scope.scope_type == SportsMarketScopeType.TENNIS_SET_GAMES:
        return _tennis_set_games_total_is_over(state, scope.scope_number, market.line)
    return False


def _tennis_moneyline_tail_state_reached(state: TennisGameState, market: SportsMarketSnapshot) -> bool:
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return False
    other_side = SportsMarketSide.AWAY if market.side == SportsMarketSide.HOME else SportsMarketSide.HOME
    side_games = state.current_set_games_for(market.side)
    other_games = state.current_set_games_for(other_side)
    if side_games is None or other_games is None:
        return False
    return (
        side_games >= 5
        and side_games - other_games >= 2
        and state.sets_won_for(market.side) - state.sets_won_for(other_side) >= 1
    )


def _tennis_set_winner_tail_state_reached(state: TennisGameState, market: SportsMarketSnapshot) -> bool:
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return False
    set_number = _tennis_set_winner_number(market)
    if set_number is None:
        return False
    if state.current_set == set_number:
        side_games = state.current_set_games_for(market.side)
        other_side = SportsMarketSide.AWAY if market.side == SportsMarketSide.HOME else SportsMarketSide.HOME
        other_games = state.current_set_games_for(other_side)
        return (
            side_games is not None
            and other_games is not None
            and side_games >= 5
            and side_games - other_games >= 2
        )
    if state.current_set is not None and state.current_set <= set_number:
        return False
    if len(state.set_scores) < set_number:
        return False
    home_games, away_games = state.set_scores[set_number - 1]
    side_games = home_games if market.side == SportsMarketSide.HOME else away_games
    other_games = away_games if market.side == SportsMarketSide.HOME else home_games
    return side_games > other_games


# ---- 状态拐点判定（私有） --------------------------------------------


def _tennis_current_set_side_near_locked(state: TennisGameState, side: SportsMarketSide) -> bool:
    """判断当前盘指定方向是否接近拿下，服务 set winner 的提前入场。"""

    if side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return False
    other_side = SportsMarketSide.AWAY if side == SportsMarketSide.HOME else SportsMarketSide.HOME
    side_games = state.current_set_games_for(side)
    other_games = state.current_set_games_for(other_side)
    return (
        side_games is not None
        and other_games is not None
        and side_games >= 5
        and side_games - other_games >= 2
        and _tennis_side_has_service_point_pressure(state, side)
    )


def _tennis_side_has_service_point_pressure(state: TennisGameState, side: SportsMarketSide) -> bool:
    """要求目标方发球且至少到 40/A，避免只凭 5-3 局分过早抢单。"""

    if state.serving_side != side.value:
        return False
    point = state.home_point if side == SportsMarketSide.HOME else state.away_point
    if point is None:
        return False
    return point.strip().upper() in {"40", "A", "AD", "ADV", "ADVANTAGE"}


def _tennis_set_winner_completed_for_side(
    state: TennisGameState,
    market: SportsMarketSnapshot,
) -> bool:
    """判断 set winner 盘口对应的目标盘是否已结束且指定方向胜出。"""

    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return False
    set_number = _tennis_set_winner_number(market)
    if set_number is None:
        return False
    if state.current_set is not None and state.current_set <= set_number:
        return False
    if len(state.set_scores) < set_number:
        return False
    home_games, away_games = state.set_scores[set_number - 1]
    side_games = home_games if market.side == SportsMarketSide.HOME else away_games
    other_games = away_games if market.side == SportsMarketSide.HOME else home_games
    return side_games > other_games


# ---- 网球总局数运算（私有） ------------------------------------------


def _tennis_set_games_total(
    state: TennisGameState,
    set_number: int | None,
) -> int | None:
    """返回指定盘已经记录到的总局数。"""

    if set_number is None:
        return None
    if len(state.set_scores) >= set_number:
        home_games, away_games = state.set_scores[set_number - 1]
        return home_games + away_games
    if state.current_set == set_number:
        home_games = state.home_current_set_games
        away_games = state.away_current_set_games
        if home_games is not None and away_games is not None:
            return home_games + away_games
    return None


def _tennis_set_games_total_is_over(
    state: TennisGameState,
    set_number: int | None,
    line: Decimal | None,
) -> bool:
    """判断指定盘总局数 Over 是否已经锁定。"""

    if line is None:
        return False
    total_games = _tennis_set_games_total(state, set_number)
    return total_games is not None and Decimal(total_games) > line


def _tennis_set_games_total_is_under_locked(
    state: TennisGameState,
    set_number: int | None,
    line: Decimal | None,
) -> bool:
    """判断指定盘已结束且 Under 已经锁定。

    当前盘还在进行时总局数只会继续增加，不能仅凭暂时低于盘口线抢 under；
    只有目标盘已进入 ``set_scores`` 的完成记录后才视为可成交机会。
    """

    if set_number is None or line is None or len(state.set_scores) < set_number:
        return False
    home_games, away_games = state.set_scores[set_number - 1]
    if not _tennis_set_score_is_final(home_games, away_games):
        return False
    return Decimal(home_games + away_games) < line


def _tennis_sets_total_is_over(state: TennisGameState, line: Decimal | None) -> bool:
    if line is None:
        return False
    completed_sets = state.home_sets_won + state.away_sets_won
    if Decimal(completed_sets) > line:
        return True
    if state.current_set is None:
        return False
    return Decimal(state.current_set) > line


def _tennis_match_total_min_final_games_is_over(state: TennisGameState, line: Decimal | None) -> bool:
    """用决胜盘最低可能最终局数判断整场总局数 Over 是否已经锁定。"""

    if line is None:
        return False
    if state.current_set is None or state.current_set < 3:
        return False
    current_home = state.home_current_set_games
    current_away = state.away_current_set_games
    if current_home is None or current_away is None:
        return False
    previous_games = max(0, state.total_games - current_home - current_away)
    minimum_current_set_games = _tennis_minimum_final_set_games(current_home, current_away)
    if minimum_current_set_games is None:
        return False
    return Decimal(previous_games + minimum_current_set_games) > line


def _tennis_minimum_final_set_games(home_games: int, away_games: int) -> int | None:
    """返回从当前局分出发，当前盘最少还会以多少总局数结束。"""

    if home_games < 0 or away_games < 0:
        return None
    if _tennis_set_score_is_final(home_games, away_games):
        return home_games + away_games
    best: int | None = None
    for final_home in range(home_games, 8):
        for final_away in range(away_games, 8):
            if not _tennis_set_score_is_final(final_home, final_away):
                continue
            total = final_home + final_away
            if best is None or total < best:
                best = total
    return best


def _tennis_set_score_is_final(home_games: int, away_games: int) -> bool:
    if home_games == 7 and away_games in {5, 6}:
        return True
    if away_games == 7 and home_games in {5, 6}:
        return True
    return (home_games >= 6 or away_games >= 6) and abs(home_games - away_games) >= 2
