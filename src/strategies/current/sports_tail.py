"""体育直播扫尾策略的纯业务模型和评估规则。

本模块只表达策略侧业务语义，不依赖 app、infra、worker，也不直接创建订单。
外部比赛状态、盘口和 orderbook 需要先转换成这里的内部对象，再由 trading hook
映射成框架统一的 ``ExtensionDecision``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping


class SportsMarketType(StrEnum):
    """体育扫尾策略支持的盘口类型。"""

    TOTALS = "totals"
    MONEYLINE = "moneyline"
    SPREADS = "spreads"


class SportsMarketFamily(StrEnum):
    """体育扫尾按结算语义划分的市场家族。"""

    SINGLE_GAME = "single_game"
    SERIES = "series"
    OUTRIGHT = "outright"
    ESPORTS = "esports"
    UNSUPPORTED = "unsupported"


class SportsMarketSide(StrEnum):
    """体育盘口方向。"""

    OVER = "over"
    UNDER = "under"
    HOME = "home"
    AWAY = "away"


class LiveGameStatus(StrEnum):
    """策略侧直播比赛状态。"""

    SCHEDULED = "scheduled"
    LIVE = "live"
    PAUSED = "paused"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"
    RETIRED = "retired"
    ENDED = "ended"
    UNKNOWN = "unknown"


class ExecutionPermission(StrEnum):
    """候选通过评估后的执行权限。"""

    RECORD_ONLY = "record_only"
    ALERT_ONLY = "alert_only"
    MANUAL_CONFIRM = "manual_confirm"
    AUTO_EXECUTE = "auto_execute"


class TailAction(StrEnum):
    """候选评估后的建议动作。"""

    REJECT = "reject"
    RECORD = "record"
    ALERT = "alert"
    MANUAL_CONFIRM = "manual_confirm"
    AUTO_EXECUTE = "auto_execute"


class SportsTailOpportunityType(StrEnum):
    """体育扫尾策略识别出的机会类型。"""

    LIVE_TAIL = "live_tail"
    ENDED_NOT_CLOSED = "ended_not_closed"
    SCALE_IN_ADVANTAGE = "scale_in_advantage"


class TailRejectReason(StrEnum):
    """体育扫尾评估拒绝原因。"""

    MISSING_LIVE_GAME_STATE = "missing_live_game_state"
    GAME_NOT_LIVE = "game_not_live"
    STALE_GAME_STATE = "stale_game_state"
    MISSING_MARKET_LINE = "missing_market_line"
    MISSING_BEST_ASK = "missing_best_ask"
    PRICE_ABOVE_MAX = "price_above_max"
    LIQUIDITY_BELOW_MIN = "liquidity_below_min"
    OUTCOME_NOT_LOCKED = "outcome_not_locked"
    MISSING_SECONDS_REMAINING = "missing_seconds_remaining"
    GAME_NOT_LATE_ENOUGH = "game_not_late_enough"
    INSUFFICIENT_LEAD = "insufficient_lead"
    INSUFFICIENT_SAFETY_MARGIN = "insufficient_safety_margin"
    UNSUPPORTED_MARKET_TYPE = "unsupported_market_type"
    UNSUPPORTED_MARKET_SIDE = "unsupported_market_side"
    SERIES_MARKET_NOT_AUTO_TRADABLE = "series_market_not_auto_tradable"
    OUTRIGHT_MARKET_NOT_AUTO_TRADABLE = "outright_market_not_auto_tradable"
    ESPORTS_MARKET_NOT_AUTO_TRADABLE = "esports_market_not_auto_tradable"
    UNSUPPORTED_MARKET_FAMILY = "unsupported_market_family"
    LIVE_SOURCE_CONFLICT = "live_source_conflict"
    MISSING_BASEBALL_STATE = "missing_baseball_state"
    BASEBALL_NOT_LATE_ENOUGH = "baseball_not_late_enough"
    BASEBALL_THREAT_ON_BASE = "baseball_threat_on_base"
    BASEBALL_OFFENSE_NOT_TRAILING = "baseball_offense_not_trailing"
    MISSING_TENNIS_STATE = "missing_tennis_state"
    TENNIS_NOT_LATE_ENOUGH = "tennis_not_late_enough"
    TENNIS_TOTALS_UNDER_NOT_SUPPORTED = "tennis_totals_under_not_supported"
    TENNIS_SET_WINNER_NOT_SUPPORTED = "tennis_set_winner_not_supported"
    TENNIS_TOTAL_SCOPE_UNSUPPORTED = "tennis_total_scope_unsupported"
    TENNIS_SPREADS_NOT_SUPPORTED = "tennis_spreads_not_supported"


@dataclass(frozen=True, slots=True)
class BaseballGameState:
    """策略评估 MLB 扫尾所需的棒球局面。"""

    current_inning: int | None = None
    inning_half: str | None = None
    outs: int | None = None
    offense_team: str | None = None
    defense_team: str | None = None
    occupied_bases: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TennisGameState:
    """策略评估网球扫尾所需的盘分、局分和即时分状态。"""

    home_sets_won: int = 0
    away_sets_won: int = 0
    current_set: int | None = None
    home_current_set_games: int | None = None
    away_current_set_games: int | None = None
    home_total_games: int = 0
    away_total_games: int = 0
    set_scores: tuple[tuple[int, int], ...] = ()
    home_point: str | None = None
    away_point: str | None = None

    @property
    def total_games(self) -> int:
        """返回当前已完成和正在进行的总局数。"""

        return self.home_total_games + self.away_total_games

    def sets_won_for(self, side: SportsMarketSide) -> int:
        """返回指定方向已赢盘数。"""

        if side == SportsMarketSide.HOME:
            return self.home_sets_won
        if side == SportsMarketSide.AWAY:
            return self.away_sets_won
        return 0

    def current_set_games_for(self, side: SportsMarketSide) -> int | None:
        """返回指定方向当前盘局数。"""

        if side == SportsMarketSide.HOME:
            return self.home_current_set_games
        if side == SportsMarketSide.AWAY:
            return self.away_current_set_games
        return None


@dataclass(frozen=True, slots=True)
class LiveGameState:
    """策略评估所需的直播比赛状态。"""

    league: str
    home_name: str
    away_name: str
    home_score: int
    away_score: int
    period: str
    status: LiveGameStatus
    seconds_remaining: int | None = None
    observed_at: datetime | None = None
    source_conflicts: tuple[Mapping[str, Any], ...] = ()
    baseball_state: BaseballGameState | None = None
    tennis_state: TennisGameState | None = None

    @property
    def total_score(self) -> int:
        """返回当前双方总分。"""

        return self.home_score + self.away_score

    def score_diff_for(self, side: SportsMarketSide) -> int:
        """返回指定方向视角下的领先分差。"""

        if side == SportsMarketSide.HOME:
            return self.home_score - self.away_score
        if side == SportsMarketSide.AWAY:
            return self.away_score - self.home_score
        return 0


@dataclass(frozen=True, slots=True)
class SportsTailPolicy:
    """体育扫尾评估策略参数。"""

    enabled_market_types: tuple[SportsMarketType, ...] = (
        SportsMarketType.TOTALS,
        SportsMarketType.MONEYLINE,
        SportsMarketType.SPREADS,
    )
    totals_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    moneyline_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    spreads_execution_permission: ExecutionPermission = ExecutionPermission.AUTO_EXECUTE
    totals_max_entry_price: Decimal = Decimal("0.99")
    moneyline_max_entry_price: Decimal = Decimal("0.97")
    spreads_max_entry_price: Decimal = Decimal("0.96")
    min_liquidity_usdc: Decimal = Decimal("1")
    max_game_state_age_seconds: int = 10
    tennis_max_game_state_age_seconds: int = 35
    max_under_seconds_remaining: int = 30
    max_moneyline_seconds_remaining: int = 180
    max_spreads_seconds_remaining: int = 120
    min_under_safety_margin: Decimal = Decimal("2")
    min_moneyline_lead: int = 6
    min_spread_safety_margin: Decimal = Decimal("2")


@dataclass(frozen=True, slots=True)
class SportsMarketSnapshot:
    """策略评估所需的体育盘口快照。"""

    market_type: SportsMarketType
    side: SportsMarketSide
    token_id: str
    line: Decimal | None
    best_ask: Decimal | None
    buyable_liquidity_usdc: Decimal
    market_family: SportsMarketFamily = SportsMarketFamily.SINGLE_GAME
    market_slug: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SportsTailCandidate:
    """统一体育扫尾候选。"""

    game: LiveGameState
    market: SportsMarketSnapshot
    reason: str
    risk_notes: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SportsTailEvaluation:
    """统一体育扫尾评估结果。"""

    accepted: bool
    action: TailAction
    reason: str
    candidate: SportsTailCandidate | None = None
    execution_permission: ExecutionPermission | None = None
    opportunity_type: SportsTailOpportunityType = SportsTailOpportunityType.LIVE_TAIL
    metadata: Mapping[str, Any] = field(default_factory=dict)


def live_game_state_from_metadata(metadata: Mapping[str, Any]) -> LiveGameState | None:
    """从策略上下文 metadata 中读取直播比赛状态。

    支持两种形态：
    - `metadata["sports_tail_game"]` 是映射对象；
    - 直接在 metadata 顶层提供 `league/home_score/away_score/status` 等字段。
    """

    raw_game = metadata.get("sports_tail_game")
    if raw_game is None:
        raw_game = metadata
    if not isinstance(raw_game, Mapping):
        return None

    try:
        home_score = int(raw_game["home_score"])
        away_score = int(raw_game["away_score"])
    except (KeyError, TypeError, ValueError):
        return None

    observed_at = _datetime_value(raw_game.get("observed_at"))
    return LiveGameState(
        league=str(raw_game.get("league") or ""),
        home_name=str(raw_game.get("home_name") or "home"),
        away_name=str(raw_game.get("away_name") or "away"),
        home_score=home_score,
        away_score=away_score,
        period=str(raw_game.get("period") or ""),
        status=_game_status(raw_game.get("status")),
        seconds_remaining=_optional_int(raw_game.get("seconds_remaining")),
        observed_at=observed_at,
        source_conflicts=_source_conflicts(raw_game.get("source_conflicts")),
        baseball_state=_baseball_state(raw_game.get("baseball_state")),
        tennis_state=_tennis_state(raw_game.get("tennis_state")),
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
    if game.status == LiveGameStatus.ENDED:
        market_reject_reason = _market_data_reject_reason(game, market, policy)
        if market_reject_reason:
            return _reject(candidate, market_reject_reason.value)
        return _evaluate_ended_not_closed(candidate, policy)

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

    if _is_tennis_game(game):
        return _evaluate_tennis_scale_in(candidate, policy)
    if market.market_type == SportsMarketType.TOTALS:
        return _evaluate_totals_scale_in(candidate, policy)
    if market.market_type == SportsMarketType.MONEYLINE:
        return _evaluate_moneyline_scale_in(candidate, policy)
    if market.market_type == SportsMarketType.SPREADS:
        return _evaluate_spreads_scale_in(candidate, policy)
    return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)


def _candidate(game: LiveGameState, market: SportsMarketSnapshot) -> SportsTailCandidate:
    return SportsTailCandidate(
        game=game,
        market=market,
        reason="sports_tail_candidate",
        metadata={
            "market_family": market.market_family.value,
            "market_type": market.market_type.value,
            "side": market.side.value,
            "line": str(market.line) if market.line is not None else None,
            "best_ask": str(market.best_ask) if market.best_ask is not None else None,
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
            },
        },
    )


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
    if market.best_ask is None:
        return TailRejectReason.MISSING_BEST_ASK
    if market.best_ask > _max_entry_price(market.market_type, policy):
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
    if market.best_ask > _max_entry_price(market.market_type, policy):
        return TailRejectReason.PRICE_ABOVE_MAX
    if market.buyable_liquidity_usdc < policy.min_liquidity_usdc:
        return TailRejectReason.LIQUIDITY_BELOW_MIN
    return None


def _evaluate_totals(
    candidate: SportsTailCandidate,
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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


def _evaluate_mlb_totals(
    candidate: SportsTailCandidate,
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
    game = candidate.game
    market = candidate.market
    if market.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    reject_reason = _mlb_side_tail_reject_reason(game, market.side)
    if reject_reason is not None:
        return _reject(candidate, reject_reason.value)
    if game.score_diff_for(market.side) < policy.min_moneyline_lead:
        return _reject(candidate, TailRejectReason.INSUFFICIENT_LEAD.value)
    return _accept(candidate, "mlb_moneyline_late_lead", policy.moneyline_execution_permission)


def _evaluate_mlb_spreads(
    candidate: SportsTailCandidate,
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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


def _evaluate_tennis_totals(
    candidate: SportsTailCandidate,
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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
    if market.side == SportsMarketSide.UNDER:
        return _reject(candidate, TailRejectReason.TENNIS_TOTALS_UNDER_NOT_SUPPORTED.value)
    if market.side != SportsMarketSide.OVER:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)
    total_scope = _tennis_total_scope(market)
    if total_scope == "match_games" and Decimal(state.total_games) > market.line:
        return _accept(candidate, "tennis_totals_over_locked", policy.totals_execution_permission)
    if total_scope == "sets" and _tennis_sets_total_is_over(state, market.line):
        return _accept(candidate, "tennis_set_totals_over_locked", policy.totals_execution_permission)
    if total_scope == "unsupported":
        return _reject(candidate, TailRejectReason.TENNIS_TOTAL_SCOPE_UNSUPPORTED.value)
    return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)


def _evaluate_tennis_moneyline(
    candidate: SportsTailCandidate,
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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


def _evaluate_tennis_set_winner(
    candidate: SportsTailCandidate,
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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
    if state.current_set is not None and state.current_set <= set_number:
        return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)
    if len(state.set_scores) < set_number:
        return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)

    home_games, away_games = state.set_scores[set_number - 1]
    side_games = home_games if market.side == SportsMarketSide.HOME else away_games
    other_games = away_games if market.side == SportsMarketSide.HOME else home_games
    if side_games > other_games:
        return _accept(candidate, "tennis_set_winner_locked", policy.moneyline_execution_permission)
    return _reject(candidate, TailRejectReason.OUTCOME_NOT_LOCKED.value)


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


def _evaluate_ended_totals(
    candidate: SportsTailCandidate,
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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


def _evaluate_ended_tennis(
    candidate: SportsTailCandidate,
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
    market = candidate.market
    state = candidate.game.tennis_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_TENNIS_STATE.value)
    if market.market_type == SportsMarketType.TOTALS:
        if market.line is None:
            return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
        total_scope = _tennis_total_scope(market)
        if total_scope == "sets":
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
        elif total_scope == "match_games":
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
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
    market = candidate.market
    state = candidate.game.tennis_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_TENNIS_STATE.value)
    set_number = _tennis_set_winner_number(market)
    if set_number is None or len(state.set_scores) < set_number:
        return _reject(candidate, TailRejectReason.TENNIS_SET_WINNER_NOT_SUPPORTED.value)
    home_games, away_games = state.set_scores[set_number - 1]
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


def _evaluate_moneyline_scale_in(
    candidate: SportsTailCandidate,
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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


def _evaluate_tennis_scale_in(
    candidate: SportsTailCandidate,
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
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
    policy: SportsTailPolicy,
) -> SportsTailEvaluation:
    """按网球盘口结算范围评估 totals 受控加仓。"""

    market = candidate.market
    state = candidate.game.tennis_state
    if state is None:
        return _reject(candidate, TailRejectReason.MISSING_TENNIS_STATE.value)
    if market.line is None:
        return _reject(candidate, TailRejectReason.MISSING_MARKET_LINE.value)
    if market.side == SportsMarketSide.UNDER:
        return _reject(candidate, TailRejectReason.TENNIS_TOTALS_UNDER_NOT_SUPPORTED.value)
    if market.side != SportsMarketSide.OVER:
        return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_SIDE.value)

    total_scope = _tennis_total_scope(market)
    if total_scope == "match_games":
        total_games = Decimal(state.total_games)
        if total_games - market.line >= Decimal("1"):
            return _accept(
                candidate,
                "scale_in_tennis_totals_over_advantage",
                policy.totals_execution_permission,
                opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
            )
        return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)
    if total_scope == "sets":
        if _tennis_sets_total_is_over(state, market.line):
            return _accept(
                candidate,
                "scale_in_tennis_set_totals_over_advantage",
                policy.totals_execution_permission,
                opportunity_type=SportsTailOpportunityType.SCALE_IN_ADVANTAGE,
            )
        return _reject(candidate, TailRejectReason.TENNIS_NOT_LATE_ENOUGH.value)
    return _reject(candidate, TailRejectReason.TENNIS_TOTAL_SCOPE_UNSUPPORTED.value)


def _accept(
    candidate: SportsTailCandidate,
    reason: str,
    permission: ExecutionPermission,
    *,
    opportunity_type: SportsTailOpportunityType = SportsTailOpportunityType.LIVE_TAIL,
) -> SportsTailEvaluation:
    return SportsTailEvaluation(
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
) -> SportsTailEvaluation:
    return SportsTailEvaluation(
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


def _max_entry_price(market_type: SportsMarketType, policy: SportsTailPolicy) -> Decimal:
    if market_type == SportsMarketType.TOTALS:
        return policy.totals_max_entry_price
    if market_type == SportsMarketType.MONEYLINE:
        return policy.moneyline_max_entry_price
    if market_type == SportsMarketType.SPREADS:
        return policy.spreads_max_entry_price
    return Decimal("0")


def _market_family_reject_reason(market_family: SportsMarketFamily) -> TailRejectReason | None:
    if market_family == SportsMarketFamily.SINGLE_GAME:
        return None
    if market_family == SportsMarketFamily.SERIES:
        return TailRejectReason.SERIES_MARKET_NOT_AUTO_TRADABLE
    if market_family == SportsMarketFamily.OUTRIGHT:
        return TailRejectReason.OUTRIGHT_MARKET_NOT_AUTO_TRADABLE
    if market_family == SportsMarketFamily.ESPORTS:
        return TailRejectReason.ESPORTS_MARKET_NOT_AUTO_TRADABLE
    return TailRejectReason.UNSUPPORTED_MARKET_FAMILY


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
    max_age_seconds = (
        policy.tennis_max_game_state_age_seconds
        if _is_tennis_game(game)
        else policy.max_game_state_age_seconds
    )
    return age_seconds > max_age_seconds


def _is_mlb_game(game: LiveGameState) -> bool:
    return game.league.strip().lower() == "mlb"


def _is_nfl_game(game: LiveGameState) -> bool:
    return game.league.strip().lower() in {"nfl", "american football"}


def _is_tennis_game(game: LiveGameState) -> bool:
    league = game.league.strip().lower()
    return game.tennis_state is not None or "tennis" in league or league in {"atp", "wta"}


def _tennis_total_scope(market: SportsMarketSnapshot) -> str:
    """识别网球 totals 盘口的结算范围。"""

    text = _normalized_market_slug(market)
    if "set total" in text or "set totals" in text or "total sets" in text:
        return "sets"
    if "match total" in text or "total games" in text:
        return "match_games"
    if market.line is not None and market.line <= Decimal("5"):
        return "unsupported"
    return "match_games"


def _tennis_sets_total_is_over(state: TennisGameState, line: Decimal | None) -> bool:
    if line is None:
        return False
    completed_sets = state.home_sets_won + state.away_sets_won
    if Decimal(completed_sets) > line:
        return True
    if state.current_set is None:
        return False
    return Decimal(state.current_set) > line


def _is_tennis_set_winner_market(market: SportsMarketSnapshot) -> bool:
    """识别网球单盘胜者盘口。"""

    text = _normalized_market_slug(market)
    return "set winner" in text or "first set winner" in text


def _tennis_set_winner_number(market: SportsMarketSnapshot) -> int | None:
    """从 market slug 识别第几盘胜者盘口。"""

    text = _normalized_market_slug(market)
    if "first set winner" in text or "1st set winner" in text or "set 1 winner" in text:
        return 1
    if "second set winner" in text or "2nd set winner" in text or "set 2 winner" in text:
        return 2
    if "third set winner" in text or "3rd set winner" in text or "set 3 winner" in text:
        return 3
    return None


def _normalized_market_slug(market: SportsMarketSnapshot) -> str:
    return (market.market_slug or "").strip().lower().replace("_", " ").replace("-", " ")


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


def _game_status(value: object) -> LiveGameStatus:
    if isinstance(value, LiveGameStatus):
        return value
    normalized = str(value or "").strip().lower()
    for status in LiveGameStatus:
        if normalized == status.value:
            return status
    return LiveGameStatus.UNKNOWN


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _source_conflicts(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _baseball_state(value: object) -> BaseballGameState | None:
    if isinstance(value, BaseballGameState):
        return value
    if not isinstance(value, Mapping):
        return None
    occupied = value.get("occupied_bases")
    occupied_bases = tuple(
        base for base in (_optional_int(item) for item in occupied)
        if base is not None
    ) if isinstance(occupied, (tuple, list)) else ()
    return BaseballGameState(
        current_inning=_optional_int(value.get("current_inning")),
        inning_half=None if value.get("inning_half") is None else str(value.get("inning_half")).strip().lower(),
        outs=_optional_int(value.get("outs")),
        offense_team=None if value.get("offense_team") is None else str(value.get("offense_team")),
        defense_team=None if value.get("defense_team") is None else str(value.get("defense_team")),
        occupied_bases=occupied_bases,
    )


def _tennis_state(value: object) -> TennisGameState | None:
    if isinstance(value, TennisGameState):
        return value
    if not isinstance(value, Mapping):
        return None
    return TennisGameState(
        home_sets_won=_optional_int(value.get("home_sets_won")) or 0,
        away_sets_won=_optional_int(value.get("away_sets_won")) or 0,
        current_set=_optional_int(value.get("current_set")),
        home_current_set_games=_optional_int(value.get("home_current_set_games")),
        away_current_set_games=_optional_int(value.get("away_current_set_games")),
        home_total_games=_optional_int(value.get("home_total_games")) or 0,
        away_total_games=_optional_int(value.get("away_total_games")) or 0,
        set_scores=_tennis_set_scores(value.get("set_scores")),
        home_point=None if value.get("home_point") is None else str(value.get("home_point")),
        away_point=None if value.get("away_point") is None else str(value.get("away_point")),
    )


def _tennis_set_scores(value: object) -> tuple[tuple[int, int], ...]:
    """解析直播源透传的每盘局分。"""

    if not isinstance(value, (tuple, list)):
        return ()
    scores: list[tuple[int, int]] = []
    for item in value:
        if isinstance(item, Mapping):
            home_games = _optional_int(item.get("home"))
            away_games = _optional_int(item.get("away"))
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            home_games = _optional_int(item[0])
            away_games = _optional_int(item[1])
        else:
            continue
        if home_games is None or away_games is None:
            continue
        scores.append((home_games, away_games))
    return tuple(scores)


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
