"""体育扫尾评估的公开入口与体育分派。

包含：
- 公开评估函数 ``evaluate_tail_opportunity`` / ``evaluate_scale_in_opportunity``
- 体育分派（按联赛选 generic / MLB / NFL / Tennis 评估器）
- 通用入场门槛检查（``_common_reject_reason`` / ``_market_data_reject_reason``）
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from decimal import Decimal

from strategies.sports_framework import (
    LiveGameState,
    LiveGameStatus,
    SportsMarketScopeType,
    SportsMarketSnapshot,
    SportsMarketType,
    is_hockey_game,
    is_mlb_game,
    is_nfl_game,
    is_soccer_game,
    is_tennis_game,
    market_scope,
)

from .core import (
    _accept,
    _candidate,
    _evaluate_basketball_first_half,
    _evaluate_ended_moneyline,
    _evaluate_ended_spreads,
    _evaluate_ended_totals,
    _evaluate_moneyline,
    _evaluate_moneyline_scale_in,
    _evaluate_spreads,
    _evaluate_spreads_scale_in,
    _evaluate_totals,
    _evaluate_totals_scale_in,
    _market_family_reject_reason,
    _reject,
)
from .mlb import (
    _evaluate_mlb_moneyline,
    _evaluate_mlb_nrfi,
    _evaluate_mlb_spreads,
    _evaluate_mlb_totals,
    is_nrfi_market,
)
from .slug import _is_tennis_set_winner_market, _market_scope_reject_reason
from .tennis import (
    _evaluate_ended_tennis,
    _evaluate_tennis_moneyline,
    _evaluate_tennis_scale_in,
    _evaluate_tennis_totals,
    _tennis_set_winner_completed_for_side,
)
from .types import (
    ExecutionPermission,
    SportsTailCandidate,
    SportsTailOpportunityType,
    TailEvaluation,
    TailPolicy,
    TailRejectReason,
)


def evaluate_tail_opportunity(
    game: LiveGameState | None,
    market: SportsMarketSnapshot,
    *,
    policy: TailPolicy,
    now: datetime | None = None,
) -> TailEvaluation:
    """评估体育盘口是否构成扫尾机会。"""

    family_reject_reason = _market_family_reject_reason(market.market_family)
    if family_reject_reason is not None:
        return _reject(
            None,
            family_reject_reason.value,
            metadata={
                "market_family": market.market_family.value,
                "market_type": market.market_type.value,
                "side": market.side.value,
                "line": str(market.line) if market.line is not None else None,
                "best_ask": str(market.best_ask) if market.best_ask is not None else None,
            },
        )

    if game is None:
        return _reject(None, TailRejectReason.MISSING_LIVE_GAME_STATE.value)

    candidate = _candidate(game, market)
    scope_reject_reason = _market_scope_reject_reason(market)
    if scope_reject_reason is not None:
        return _reject(candidate, scope_reject_reason.value)
    if game.status == LiveGameStatus.ENDED:
        market_reject_reason = _market_data_reject_reason(game, market, policy)
        if market_reject_reason:
            return _reject(candidate, market_reject_reason.value)
        return _evaluate_ended_not_closed(candidate, policy)

    common_reject_reason = _common_reject_reason(game, market, policy, now=now)
    if common_reject_reason:
        return _reject(candidate, common_reject_reason.value)

    # 篮球上半场盘口：半场结束即由 q1+q2 锁定，独立于整场 sport 评估器。
    if market_scope(market).scope_type == SportsMarketScopeType.BASKETBALL_FIRST_HALF:
        return _evaluate_basketball_first_half(candidate, policy)

    if is_mlb_game(game):
        if market.market_type == SportsMarketType.TOTALS:
            return _evaluate_mlb_totals(candidate, policy)
        if market.market_type == SportsMarketType.MONEYLINE:
            return _evaluate_mlb_moneyline(candidate, policy)
        if market.market_type == SportsMarketType.SPREADS:
            return _evaluate_mlb_spreads(candidate, policy)
        if market.market_type == SportsMarketType.BINARY_PROP and is_nrfi_market(market):
            return _evaluate_mlb_nrfi(candidate, policy)
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)

    if is_nfl_game(game):
        return _evaluate_nfl_manual_review(candidate, policy)

    if market.market_type == SportsMarketType.BINARY_PROP:
        return _reject(candidate, "binary_prop_no_tail_model")

    if is_tennis_game(game):
        if market.market_type == SportsMarketType.TOTALS:
            return _evaluate_tennis_totals(candidate, policy)
        if market.market_type == SportsMarketType.MONEYLINE:
            return _evaluate_tennis_moneyline(candidate, policy)
        if market.market_type == SportsMarketType.SPREADS:
            return _reject(candidate, TailRejectReason.TENNIS_SPREADS_NOT_SUPPORTED.value)
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)

    if market.market_type == SportsMarketType.TOTALS:
        return _evaluate_totals(candidate, policy)
    if market.market_type == SportsMarketType.MONEYLINE:
        return _evaluate_moneyline(candidate, _sport_moneyline_policy(game, policy))
    if market.market_type == SportsMarketType.SPREADS:
        return _evaluate_spreads(candidate, policy)
    return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)


def evaluate_scale_in_opportunity(
    game: LiveGameState | None,
    market: SportsMarketSnapshot,
    *,
    policy: TailPolicy,
    now: datetime | None = None,
) -> TailEvaluation:
    """评估已有持仓是否达到受控加仓所需的更严格优势状态。"""

    family_reject_reason = _market_family_reject_reason(market.market_family)
    if family_reject_reason is not None:
        return _reject(
            None,
            family_reject_reason.value,
            metadata={
                "market_family": market.market_family.value,
                "market_type": market.market_type.value,
                "side": market.side.value,
                "line": str(market.line) if market.line is not None else None,
                "best_ask": str(market.best_ask) if market.best_ask is not None else None,
            },
        )
    if game is None:
        return _reject(None, TailRejectReason.MISSING_LIVE_GAME_STATE.value)

    candidate = _candidate(game, market)
    scope_reject_reason = _market_scope_reject_reason(market)
    if scope_reject_reason is not None:
        return _reject(candidate, scope_reject_reason.value)
    if game.status == LiveGameStatus.ENDED:
        market_reject_reason = _market_data_reject_reason(game, market, policy)
        if market_reject_reason:
            return _reject(candidate, market_reject_reason.value)
        ended = _evaluate_ended_not_closed(candidate, policy)
        if not ended.accepted:
            return ended
        return _accept(
            candidate,
            f"scale_in_{ended.reason.removeprefix('ended_not_closed_')}",
            ended.execution_permission or ExecutionPermission.AUTO_EXECUTE,
            opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
        )

    common_reject_reason = _common_reject_reason(game, market, policy, now=now)
    if common_reject_reason:
        return _reject(candidate, common_reject_reason.value)

    if is_tennis_game(game):
        return _evaluate_tennis_scale_in(candidate, policy)
    if market.market_type == SportsMarketType.TOTALS:
        return _evaluate_totals_scale_in(candidate, policy)
    if market.market_type == SportsMarketType.MONEYLINE:
        return _evaluate_moneyline_scale_in(candidate, _sport_moneyline_policy(game, policy))
    if market.market_type == SportsMarketType.SPREADS:
        return _evaluate_spreads_scale_in(candidate, policy)
    return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)


# ---- 内部工具 -------------------------------------------------------


def _evaluate_ended_not_closed(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """用最终比分判断已结束但未封盘 market 的确定性方向。"""

    market = candidate.market
    if is_tennis_game(candidate.game):
        return _evaluate_ended_tennis(candidate, policy)
    if market.market_type == SportsMarketType.TOTALS:
        return _evaluate_ended_totals(candidate, policy)
    if market.market_type == SportsMarketType.MONEYLINE:
        return _evaluate_ended_moneyline(candidate, policy)
    if market.market_type == SportsMarketType.SPREADS:
        return _evaluate_ended_spreads(candidate, policy)
    return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)


def _evaluate_nfl_manual_review(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    if candidate.market.market_type == SportsMarketType.TOTALS:
        evaluation = _evaluate_totals(candidate, policy)
    elif candidate.market.market_type == SportsMarketType.MONEYLINE:
        evaluation = _evaluate_moneyline(candidate, policy)
    elif candidate.market.market_type == SportsMarketType.SPREADS:
        evaluation = _evaluate_spreads(candidate, policy)
    else:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)
    if not evaluation.accepted:
        return evaluation
    return _accept(candidate, "nfl_requires_manual_review", ExecutionPermission.MANUAL_CONFIRM)


def _has_score_conflict(game: LiveGameState) -> bool:
    """只有分数字段冲突才真正影响决策；status/period 过渡冲突（如 live↔unknown）忽略。"""
    _SCORE_FIELDS = frozenset({"home_score", "away_score", "home_goals", "away_goals", "score"})
    return any(c.get("field") in _SCORE_FIELDS for c in game.source_conflicts)


def _common_reject_reason(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: TailPolicy,
    *,
    now: datetime | None,
) -> TailRejectReason | None:
    if game.status != LiveGameStatus.LIVE:
        return TailRejectReason.GAME_NOT_LIVE
    if _has_score_conflict(game):
        return TailRejectReason.LIVE_SOURCE_CONFLICT
    if _is_stale(game, policy, now=now):
        return TailRejectReason.STALE_GAME_STATE
    # BINARY_PROP 无传统盘口价格结构，跳过 ask/流动性检查；
    # 后续由体育专属评估器（目前是 binary_prop_no_tail_model）统一处理。
    if market.market_type == SportsMarketType.BINARY_PROP:
        return None
    if market.best_ask is None:
        return TailRejectReason.MISSING_BEST_ASK
    if market.best_ask < policy.min_entry_price:
        return TailRejectReason.PRICE_BELOW_MIN
    if market.best_ask > _max_entry_price(game, market, policy):
        return TailRejectReason.PRICE_ABOVE_MAX
    if market.buyable_liquidity_usdc < policy.min_liquidity_usdc:
        return TailRejectReason.LIQUIDITY_BELOW_MIN
    return None


def _market_data_reject_reason(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: TailPolicy,
) -> TailRejectReason | None:
    """检查不依赖比赛是否 live 的盘口和来源门槛。"""

    if _has_score_conflict(game):
        return TailRejectReason.LIVE_SOURCE_CONFLICT
    if market.best_ask is None:
        return TailRejectReason.MISSING_BEST_ASK
    if market.best_ask < policy.min_entry_price:
        return TailRejectReason.PRICE_BELOW_MIN
    if market.best_ask > _max_entry_price(game, market, policy):
        return TailRejectReason.PRICE_ABOVE_MAX
    if market.buyable_liquidity_usdc < policy.min_liquidity_usdc:
        return TailRejectReason.LIQUIDITY_BELOW_MIN
    return None


def _max_entry_price(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: TailPolicy,
) -> Decimal:
    if (
        is_tennis_game(game)
        and _is_tennis_set_winner_market(market)
        and game.tennis_state is not None
        and _tennis_set_winner_completed_for_side(game.tennis_state, market)
    ):
        return policy.tennis_locked_moneyline_max_entry_price
    if market.market_type == SportsMarketType.TOTALS:
        return policy.totals_max_entry_price
    if market.market_type == SportsMarketType.MONEYLINE:
        return policy.moneyline_max_entry_price
    if market.market_type == SportsMarketType.SPREADS:
        return policy.spreads_max_entry_price
    return Decimal("0")


def _is_stale(
    game: LiveGameState,
    policy: TailPolicy,
    *,
    now: datetime | None,
) -> bool:
    if game.observed_at is None:
        return False
    current_time = now or datetime.now(timezone.utc)
    observed_at = game.observed_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age_seconds = (current_time - observed_at).total_seconds()
    if is_tennis_game(game):
        max_age_seconds = policy.tennis_max_game_state_age_seconds
    elif is_mlb_game(game) and game.baseball_state is not None:
        max_age_seconds = policy.baseball_max_game_state_age_seconds
    else:
        max_age_seconds = policy.max_game_state_age_seconds
    return age_seconds > max_age_seconds



def _sport_moneyline_policy(game: LiveGameState, policy: TailPolicy) -> TailPolicy:
    """为低分运动覆盖 min_moneyline_lead，避免足球/冰球被 6 分 lead 要求完全封住。"""

    if is_soccer_game(game):
        return dataclasses.replace(policy, min_moneyline_lead=policy.soccer_min_moneyline_lead)
    if is_hockey_game(game):
        return dataclasses.replace(policy, min_moneyline_lead=policy.hockey_min_moneyline_lead)
    return policy
