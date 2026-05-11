"""MLB / 棒球类联赛专属评估、状态判定与 tail-state-reached 工具。"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.sports_live import BaseballGameState
from strategies.sports_framework import (
    LiveGameState,
    SportsMarketSide,
    SportsMarketSnapshot,
    SportsMarketType,
)

from .core import _accept, _reject
from .types import (
    SportsTailCandidate,
    TailEvaluation,
    TailPolicy,
    TailRejectReason,
)


def _evaluate_mlb_totals(
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
        reject_reason = _mlb_low_scoring_tail_reject_reason(game)
        if reject_reason is not None:
            return _reject(candidate, reject_reason.value)
        if market.line - total_score >= policy.min_under_safety_margin:
            return _accept(candidate, "mlb_totals_under_late_low_risk", policy.totals_execution_permission)
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


def _evaluate_mlb_moneyline(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    early_eighth_threat = _mlb_eighth_moneyline_threat_reject_reason(game, market.side, policy)
    if early_eighth_threat is not None:
        return _reject(candidate, early_eighth_threat.value)
    early_eighth = _mlb_eighth_moneyline_lead_reached(game, market.side, policy)
    reject_reason = None if early_eighth else _mlb_side_tail_reject_reason(game, market.side)
    if reject_reason is not None:
        return _reject(candidate, reject_reason.value)
    if early_eighth:
        return _accept(candidate, "mlb_moneyline_eighth_lead", policy.moneyline_execution_permission)
    if game.score_diff_for(market.side) < policy.min_moneyline_lead:
        return _reject(candidate, TailRejectReason.INSUFFICIENT_LEAD.value)
    return _accept(candidate, "mlb_moneyline_late_lead", policy.moneyline_execution_permission)


def _evaluate_mlb_spreads(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    if market.line is None:
        return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
    reject_reason = _mlb_side_tail_reject_reason(game, market.side)
    if reject_reason is not None:
        return _reject(candidate, reject_reason.value)
    safety_margin = Decimal(game.score_diff_for(market.side)) + market.line
    if safety_margin < policy.min_spread_safety_margin:
        return _reject(candidate, TailRejectReason.INSUFFICIENT_SAFETY_MARGIN.value)
    return _accept(candidate, "mlb_spreads_late_cover", policy.spreads_execution_permission)


def _mlb_tail_state_reached(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: TailPolicy,
) -> bool:
    """按 MLB 的局数、出局和垒上状态判断是否已经进入尾盘。"""

    if market.market_type == SportsMarketType.TOTALS:
        if market.line is None:
            return False
        total_score = Decimal(game.total_score)
        if market.side == SportsMarketSide.OVER:
            return total_score > market.line
        return (
            market.side == SportsMarketSide.UNDER
            and _mlb_low_scoring_tail_reject_reason(game) is None
            and market.line - total_score >= policy.min_under_safety_margin
        )
    if market.market_type == SportsMarketType.MONEYLINE:
        return (
            market.side in {SportsMarketSide.HOME, SportsMarketSide.AWAY}
            and _mlb_side_tail_reject_reason(game, market.side) is None
            and game.score_diff_for(market.side) >= policy.min_moneyline_lead
        )
    if market.market_type == SportsMarketType.SPREADS:
        if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY} or market.line is None:
            return False
        safety_margin = Decimal(game.score_diff_for(market.side)) + market.line
        return (
            _mlb_side_tail_reject_reason(game, market.side) is None
            and safety_margin >= policy.min_spread_safety_margin
        )
    return False


def _mlb_side_tail_reject_reason(
    game: LiveGameState,
    side: SportsMarketSide,
) -> TailRejectReason | None:
    state = game.baseball_state
    if state is None:
        return TailRejectReason.MISSING_BASEBALL_STATE
    if (state.current_inning or 0) < 9 or (state.outs or 0) < 2:
        return TailRejectReason.BASEBALL_NOT_LATE_ENOUGH
    if state.occupied_bases:
        return TailRejectReason.BASEBALL_THREAT_ON_BASE
    offense_side = _baseball_offense_side(game, state)
    if offense_side is None:
        return TailRejectReason.MISSING_BASEBALL_STATE
    if offense_side == side:
        return TailRejectReason.BASEBALL_OFFENSE_NOT_TRAILING
    return None


def _mlb_eighth_moneyline_lead_reached(
    game: LiveGameState,
    side: SportsMarketSide,
    policy: TailPolicy,
) -> bool:
    """识别 MLB 第 8 局后段的受控 moneyline 领先方机会。

    第 8 局仍有对手后续进攻机会，因此只放行领先方、至少一出局、领先达到
    单独阈值且没有二/三垒得分威胁的场景；第 9 局继续使用更强的锁定规则。
    """

    state = game.baseball_state
    if state is None:
        return False
    if state.current_inning != 8 or (state.outs or 0) < 1:
        return False
    if game.score_diff_for(side) < policy.mlb_eighth_moneyline_min_lead:
        return False
    occupied_bases = {int(base) for base in state.occupied_bases}
    if occupied_bases.intersection({2, 3}):
        return False
    return side in {SportsMarketSide.HOME, SportsMarketSide.AWAY}


def _mlb_eighth_moneyline_threat_reject_reason(
    game: LiveGameState,
    side: SportsMarketSide,
    policy: TailPolicy,
) -> TailRejectReason | None:
    """第 8 局早期 moneyline 窗口内存在二/三垒威胁时给出可审计拒绝原因。"""

    state = game.baseball_state
    if state is None:
        return None
    if state.current_inning != 8 or (state.outs or 0) < 1:
        return None
    if game.score_diff_for(side) < policy.mlb_eighth_moneyline_min_lead:
        return None
    occupied_bases = {int(base) for base in state.occupied_bases}
    if occupied_bases.intersection({2, 3}):
        return TailRejectReason.BASEBALL_THREAT_ON_BASE
    return None


def _mlb_low_scoring_tail_reject_reason(game: LiveGameState) -> TailRejectReason | None:
    state = game.baseball_state
    if state is None:
        return TailRejectReason.MISSING_BASEBALL_STATE
    if (state.current_inning or 0) < 9 or (state.outs or 0) < 2:
        return TailRejectReason.BASEBALL_NOT_LATE_ENOUGH
    if str(state.inning_half or "").strip().lower() != "bottom":
        return TailRejectReason.BASEBALL_NOT_LATE_ENOUGH
    if state.occupied_bases:
        return TailRejectReason.BASEBALL_THREAT_ON_BASE
    return None


def _baseball_offense_side(
    game: LiveGameState,
    state: BaseballGameState,
) -> SportsMarketSide | None:
    offense_team = _normalize_team_name(state.offense_team)
    if offense_team:
        if offense_team in _team_name_aliases(game.home_name):
            return SportsMarketSide.HOME
        if offense_team in _team_name_aliases(game.away_name):
            return SportsMarketSide.AWAY
    inning_half = str(state.inning_half or "").strip().lower()
    if inning_half == "top":
        return SportsMarketSide.AWAY
    if inning_half == "bottom":
        return SportsMarketSide.HOME
    return None


def _team_name_aliases(name: str) -> set[str]:
    normalized = _normalize_team_name(name)
    tokens = normalized.split()
    return {
        normalized,
        tokens[-1] if tokens else "",
    } - {""}


def _normalize_team_name(value: str | None) -> str:
    return " ".join(str(value or "").lower().replace("&", " and ").split())
