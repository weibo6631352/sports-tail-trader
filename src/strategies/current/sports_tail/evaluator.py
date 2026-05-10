"""体育扫尾评估的公开入口与体育分派。

包含：
- 公开评估函数 ``evaluate_tail_opportunity`` / ``evaluate_scale_in_opportunity``
- 体育分派（按联赛选 generic / MLB / NFL / Tennis 评估器）
- 通用入场门槛检查（``_common_reject_reason`` / ``_market_data_reject_reason``）
- 价格上限和 endDate 粗筛绕过
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .core import (
    _accept,
    _candidate,
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
    _standard_tail_state_reached,
)
from .leagues import _is_mlb_game, _is_nfl_game, _is_tennis_game
from .mlb import (
    _evaluate_mlb_moneyline,
    _evaluate_mlb_spreads,
    _evaluate_mlb_totals,
    _mlb_tail_state_reached,
)
from .slug import _is_tennis_set_winner_market, _market_scope_reject_reason
from .tennis import (
    _evaluate_ended_tennis,
    _evaluate_tennis_moneyline,
    _evaluate_tennis_scale_in,
    _evaluate_tennis_totals,
    _tennis_set_winner_completed_for_side,
    _tennis_tail_state_reached,
)
from .types import (
    ExecutionPermission,
    LiveGameState,
    LiveGameStatus,
    SportsMarketSnapshot,
    SportsMarketType,
    SportsTailCandidate,
    SportsTailEvaluation,
    SportsTailOpportunityType,
    SportsTailPolicy,
    TailRejectReason,
)


def evaluate_tail_opportunity(
    game: LiveGameState | None,
    market: SportsMarketSnapshot,
    *,
    policy: SportsTailPolicy,
    now: datetime | None = None,
) -> SportsTailEvaluation:
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

    if _market_end_too_far(market, policy, now=now) and not _can_bypass_market_end_window(game, market, policy):
        return _reject(candidate, TailRejectReason.MARKET_END_TOO_FAR.value)

    common_reject_reason = _common_reject_reason(game, market, policy, now=now)
    if common_reject_reason:
        return _reject(candidate, common_reject_reason.value)

    if _is_mlb_game(game):
        if market.market_type == SportsMarketType.TOTALS:
            return _evaluate_mlb_totals(candidate, policy)
        if market.market_type == SportsMarketType.MONEYLINE:
            return _evaluate_mlb_moneyline(candidate, policy)
        if market.market_type == SportsMarketType.SPREADS:
            return _evaluate_mlb_spreads(candidate, policy)
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)

    if _is_nfl_game(game):
        return _evaluate_nfl_manual_review(candidate, policy)

    if market.market_type == SportsMarketType.BINARY_PROP:
        return _accept(candidate, "binary_prop_requires_specific_model", ExecutionPermission.RECORD_ONLY)

    if _is_tennis_game(game):
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
        return _evaluate_moneyline(candidate, policy)
    if market.market_type == SportsMarketType.SPREADS:
        return _evaluate_spreads(candidate, policy)
    if market.market_type == SportsMarketType.BINARY_PROP:
        return _accept(candidate, "binary_prop_requires_specific_model", ExecutionPermission.RECORD_ONLY)
    return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)


def evaluate_scale_in_opportunity(
    game: LiveGameState | None,
    market: SportsMarketSnapshot,
    *,
    policy: SportsTailPolicy,
    now: datetime | None = None,
) -> SportsTailEvaluation:
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

    if _market_end_too_far(market, policy, now=now) and not _can_bypass_market_end_window(game, market, policy):
        return _reject(candidate, TailRejectReason.MARKET_END_TOO_FAR.value)

    common_reject_reason = _common_reject_reason(game, market, policy, now=now)
    if common_reject_reason:
        return _reject(candidate, common_reject_reason.value)

    if _is_tennis_game(game):
        return _evaluate_tennis_scale_in(candidate, policy)
    if market.market_type == SportsMarketType.TOTALS:
        return _evaluate_totals_scale_in(candidate, policy)
    if market.market_type == SportsMarketType.MONEYLINE:
        return _evaluate_moneyline_scale_in(candidate, policy)
    if market.market_type == SportsMarketType.SPREADS:
        return _evaluate_spreads_scale_in(candidate, policy)
    return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)


# ---- 内部工具 -------------------------------------------------------


def _evaluate_ended_not_closed(
    candidate: SportsTailCandidate,
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
    """用最终比分判断已结束但未封盘 market 的确定性方向。"""

    market = candidate.market
    if _is_tennis_game(candidate.game):
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
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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


def _common_reject_reason(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: SportsTailPolicy,
    *,
    now: datetime | None,
) -> TailRejectReason | None:
    if game.status != LiveGameStatus.LIVE:
        return TailRejectReason.GAME_NOT_LIVE
    if game.source_conflicts:
        return TailRejectReason.LIVE_SOURCE_CONFLICT
    if _is_stale(game, policy, now=now):
        return TailRejectReason.STALE_GAME_STATE
    if market.market_type == SportsMarketType.BINARY_PROP:
        return None
    if market.best_ask is None:
        return TailRejectReason.MISSING_BEST_ASK
    if market.best_ask > _max_entry_price(game, market, policy):
        return TailRejectReason.PRICE_ABOVE_MAX
    if market.buyable_liquidity_usdc < policy.min_liquidity_usdc:
        return TailRejectReason.LIQUIDITY_BELOW_MIN
    return None


def _market_data_reject_reason(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: SportsTailPolicy,
) -> TailRejectReason | None:
    """检查不依赖比赛是否 live 的盘口和来源门槛。"""

    if game.source_conflicts:
        return TailRejectReason.LIVE_SOURCE_CONFLICT
    if market.best_ask is None:
        return TailRejectReason.MISSING_BEST_ASK
    if market.best_ask > _max_entry_price(game, market, policy):
        return TailRejectReason.PRICE_ABOVE_MAX
    if market.buyable_liquidity_usdc < policy.min_liquidity_usdc:
        return TailRejectReason.LIQUIDITY_BELOW_MIN
    return None


def _max_entry_price(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: SportsTailPolicy,
) -> Decimal:
    if (
        _is_tennis_game(game)
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
    policy: SportsTailPolicy,
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
    if _is_tennis_game(game):
        max_age_seconds = policy.tennis_max_game_state_age_seconds
    elif _is_mlb_game(game) and game.baseball_state is not None:
        max_age_seconds = policy.baseball_max_game_state_age_seconds
    else:
        max_age_seconds = policy.max_game_state_age_seconds
    return age_seconds > max_age_seconds


def _market_end_too_far(
    market: SportsMarketSnapshot,
    policy: SportsTailPolicy,
    *,
    now: datetime | None,
) -> bool:
    """判断 Polymarket 封盘时间是否仍明显早于扫尾窗口。

    该规则只用于 live 尾盘候选的粗筛；已结束但未封盘的机会在调用侧提前处理，
    缺失封盘时间则不在这里拒绝，避免因 Gamma 字段缺失错过真实尾盘。
    """

    if market.market_end_date is None or policy.max_market_end_seconds <= 0:
        return False
    current_time = now or datetime.now(timezone.utc)
    market_end = market.market_end_date
    if market_end.tzinfo is None:
        market_end = market_end.replace(tzinfo=timezone.utc)
    return (market_end.astimezone(timezone.utc) - current_time.astimezone(timezone.utc)).total_seconds() > (
        policy.max_market_end_seconds
    )


def _can_bypass_market_end_window(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: SportsTailPolicy,
) -> bool:
    """判断结果已数学锁定的 live 盘口是否可绕过 endDate 粗筛。

    Polymarket 体育 ``endDate`` 经常是结算展示日期，不等同于封盘时间。对
    已经达到策略尾盘条件的盘口，不能只因 endDate 很远就丢弃；否则会等到
    盘口完全单边化后才尝试入场。
    """

    if game.status != LiveGameStatus.LIVE:
        return False
    if _is_tennis_game(game):
        return _tennis_tail_state_reached(game, market)
    if _is_mlb_game(game):
        # MLB/KBO 等棒球市场的 Gamma endDate 常是结算展示日期，不是比赛封盘时间。
        # 已拿到结构化局面时，应由局数、出局数、垒上状态和分差决定是否可入场。
        if game.baseball_state is not None:
            return True
        return _mlb_tail_state_reached(game, market, policy)
    return _standard_tail_state_reached(game, market, policy)
