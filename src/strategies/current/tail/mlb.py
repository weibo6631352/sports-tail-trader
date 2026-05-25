"""MLB / 棒球类联赛专属评估、状态判定与 tail-state-reached 工具。"""

from __future__ import annotations

from decimal import Decimal

from polymarket_trader.domain.sports_live import BaseballGameState
from strategies.current._shared.team_normalize import normalize_team_name
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
        reject_reason = _mlb_under_totals_reject_reason(game, market, policy)
        if reject_reason is not None:
            return _reject(candidate, reject_reason.value)
        return _accept(candidate, "mlb_totals_under_margin_scaled", policy.totals_execution_permission)
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


def _evaluate_mlb_moneyline(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    # 第 9 局及延长赛：领先差达标且无二/三垒威胁 → 早期接受（0 出局即可，价格通常仍 < 0.97）
    if _mlb_ninth_moneyline_lead_reached(game, market.side, policy):
        return _accept(candidate, "mlb_moneyline_ninth_lead", policy.moneyline_execution_permission)
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


def is_nrfi_market(market: SportsMarketSnapshot) -> bool:
    """识别 NRFI（No Runs First Inning，首局无得分）盘口。"""

    return "nrfi" in (market.market_slug or "").lower()


def _evaluate_mlb_nrfi(
    candidate: SportsTailCandidate,
    policy: TailPolicy,
) -> TailEvaluation:
    """评估 NRFI（首局无得分）二元盘口。

    NRFI=Yes：首局双方均 0 分。首局结束且确认 0 分才锁定。
    NRFI=No：首局有任意得分。一旦某队首局得分 ≥1 即锁定（可早于首局结束）。
    binary_prop 在通用门禁里跳过了 ask/流动性检查，这里自查入场价。
    """

    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.YES, SportsMarketSide.NO}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    state = game.baseball_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_BASEBALL_STATE.value)
    if market.best_ask is None:
        return _reject(candidate, TailRejectReason.MISSING_BEST_ASK.value)
    # price/liquidity 入场 gate 已删——宽进严管，持仓策略接管止盈止损。

    home1 = state.home_inning_runs[0] if state.home_inning_runs else None
    away1 = state.away_inning_runs[0] if state.away_inning_runs else None
    first_inning_complete = (state.current_inning or 0) > 1

    # NRFI No：确认有得分即锁定（数据须真实存在，缺失不算）。
    scored = (home1 is not None and home1 >= 1) or (away1 is not None and away1 >= 1)
    if scored:
        if market.side == SportsMarketSide.NO:
            return _accept(candidate, "mlb_nrfi_no_locked", policy.totals_execution_permission)
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)

    # NRFI Yes：首局结束 + 双方首局得分数据齐全且均为 0 才锁定。
    if first_inning_complete:
        if home1 is None or away1 is None:
            return _reject(candidate, TailRejectReason.MISSING_BASEBALL_STATE.value)
        if market.side == SportsMarketSide.YES:
            return _accept(candidate, "mlb_nrfi_yes_locked", policy.totals_execution_permission)
        return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)

    return _reject(candidate, TailRejectReason.BASEBALL_FIRST_INNING_NOT_COMPLETE.value)


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
            and _mlb_under_totals_reject_reason(game, market, policy) is None
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


def _mlb_ninth_moneyline_lead_reached(
    game: LiveGameState,
    side: SportsMarketSide,
    policy: TailPolicy,
) -> bool:
    """识别 MLB 第 9 局及延长赛的 moneyline 领先方机会（0 出局即可触发）。

    第 8 局规则（outs≥1）的高阈值版本：第 9 局起价格通常已升至 0.93-0.97，
    需要更大领先（默认 3 分）确保正期望。二/三垒有人时跑垒威胁显著增大不开仓。
    """

    state = game.baseball_state
    if state is None:
        return False
    if (state.current_inning or 0) < 9:
        return False
    if game.score_diff_for(side) < policy.mlb_ninth_moneyline_min_lead:
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


def _mlb_under_totals_reject_reason(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: TailPolicy,
) -> TailRejectReason | None:
    """MLB Under 总分入场门禁：所需 safety margin 随局数缩放。

    越早的局剩余得分机会越多，要求的安全边际越大：9 局用基准
    ``min_under_safety_margin``，每往前一局额外加 ``mlb_under_inning_margin_step``。
    margin（盘口线 − 当前总分）足够大时 Under 在该局已基本锁定，不必死等
    九局两出局——配合提前止盈做准量化盈利。早于 ``mlb_under_min_inning`` 一律拒绝。
    """

    state = game.baseball_state
    if state is None:
        return TailRejectReason.MISSING_BASEBALL_STATE
    if market.line is None:
        return TailRejectReason.MISSING_MARKET_LINE
    inning = state.current_inning or 0
    if inning < policy.mlb_under_min_inning:
        return TailRejectReason.BASEBALL_NOT_LATE_ENOUGH
    required_margin = policy.min_under_safety_margin + (
        policy.mlb_under_inning_margin_step * Decimal(max(0, 9 - inning))
    )
    safety_margin = market.line - Decimal(game.total_score)
    if safety_margin < required_margin:
        return TailRejectReason.OUTCOME_NOT_LOCKED
    return None


def _baseball_offense_side(
    game: LiveGameState,
    state: BaseballGameState,
) -> SportsMarketSide | None:
    offense_team = normalize_team_name(state.offense_team)
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
    normalized = normalize_team_name(name)
    tokens = normalized.split()
    return {
        normalized,
        tokens[-1] if tokens else "",
    } - {""}
