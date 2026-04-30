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
    BINARY_PROP = "binary_prop"


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
    YES = "yes"
    NO = "no"


class SportsMarketScopeType(StrEnum):
    """盘口结算范围。"""

    FULL_GAME = "full_game"
    TENNIS_MATCH_GAMES = "tennis_match_games"
    TENNIS_TOTAL_SETS = "tennis_total_sets"
    TENNIS_SET_GAMES = "tennis_set_games"
    UNSUPPORTED_PERIOD = "unsupported_period"


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
    MARKET_END_TOO_FAR = "market_end_too_far"
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
    UNSUPPORTED_MARKET_SCOPE = "unsupported_market_scope"
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
    first_to_serve: str | None = None
    serving_side: str | None = None

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
    tennis_locked_moneyline_max_entry_price: Decimal = Decimal("0.995")
    spreads_max_entry_price: Decimal = Decimal("0.96")
    min_liquidity_usdc: Decimal = Decimal("1")
    max_game_state_age_seconds: int = 10
    baseball_max_game_state_age_seconds: int = 45
    tennis_max_game_state_age_seconds: int = 35
    max_market_end_seconds: int = 3600
    max_under_seconds_remaining: int = 30
    max_moneyline_seconds_remaining: int = 180
    max_spreads_seconds_remaining: int = 120
    min_under_safety_margin: Decimal = Decimal("2")
    min_moneyline_lead: int = 6
    mlb_eighth_moneyline_min_lead: int = 2
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
    market_end_date: datetime | None = None
    scope_type: SportsMarketScopeType = SportsMarketScopeType.FULL_GAME
    scope_number: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SportsMarketScope:
    """结构化盘口结算范围。"""

    scope_type: SportsMarketScopeType
    scope_number: int | None = None


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
            "scope_type": _market_scope(market).scope_type.value,
            "scope_number": _market_scope(market).scope_number,
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


def _standard_tail_state_reached(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: SportsTailPolicy,
) -> bool:
    """复用常规运动尾盘条件判断是否可忽略 Gamma 的远期 endDate。"""

    if market.market_type == SportsMarketType.TOTALS:
        return _standard_totals_tail_state_reached(game, market, policy)
    if market.market_type == SportsMarketType.MONEYLINE:
        return _standard_moneyline_tail_state_reached(game, market, policy)
    if market.market_type == SportsMarketType.SPREADS:
        return _standard_spreads_tail_state_reached(game, market, policy)
    return False


def _standard_totals_tail_state_reached(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: SportsTailPolicy,
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
    policy: SportsTailPolicy,
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
    policy: SportsTailPolicy,
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


def _mlb_tail_state_reached(
    game: LiveGameState,
    market: SportsMarketSnapshot,
    policy: SportsTailPolicy,
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


def _tennis_tail_state_reached(game: LiveGameState, market: SportsMarketSnapshot) -> bool:
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


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _is_mlb_game(game: LiveGameState) -> bool:
    league = game.league.strip().lower()
    return league in {"mlb", "kbo", "baseball", "korean baseball", "korea baseball organization"}


def _is_nfl_game(game: LiveGameState) -> bool:
    return game.league.strip().lower() in {"nfl", "american football"}


def _is_tennis_game(game: LiveGameState) -> bool:
    league = game.league.strip().lower()
    return game.tennis_state is not None or "tennis" in league or league in {"atp", "wta"}


def _market_scope(market: SportsMarketSnapshot) -> SportsMarketScope:
    """返回盘口的结构化结算范围。"""

    if market.scope_type != SportsMarketScopeType.FULL_GAME or market.scope_number is not None:
        return SportsMarketScope(market.scope_type, market.scope_number)
    if market.market_type == SportsMarketType.TOTALS:
        return _totals_market_scope(market)
    return SportsMarketScope(SportsMarketScopeType.FULL_GAME)


def _tennis_total_scope(market: SportsMarketSnapshot) -> SportsMarketScope:
    """返回网球 totals 的结算范围，缺省按整场总局数处理。"""

    scope = _market_scope(market)
    if scope.scope_type == SportsMarketScopeType.FULL_GAME:
        return SportsMarketScope(SportsMarketScopeType.TENNIS_MATCH_GAMES)
    return scope


def _market_scope_reject_reason(market: SportsMarketSnapshot) -> TailRejectReason | None:
    """返回当前策略明确不能自动结算的盘口范围拒绝原因。"""

    scope = _market_scope(market)
    if scope.scope_type == SportsMarketScopeType.UNSUPPORTED_PERIOD:
        return (
            TailRejectReason.TENNIS_TOTAL_SCOPE_UNSUPPORTED
            if _is_tennis_scope_candidate(market)
            else TailRejectReason.UNSUPPORTED_MARKET_SCOPE
        )
    return None


def _is_tennis_scope_candidate(market: SportsMarketSnapshot) -> bool:
    text = _normalized_market_slug(market)
    return "tennis" in text or "atp" in text or "wta" in text


def _totals_market_scope(market: SportsMarketSnapshot) -> SportsMarketScope:
    """识别 totals 盘口的结算范围。"""

    text = _normalized_market_slug(market)
    set_number = _tennis_set_games_total_number(text)
    if set_number is not None:
        return SportsMarketScope(SportsMarketScopeType.TENNIS_SET_GAMES, set_number)
    if _is_unsupported_period_total(text):
        return SportsMarketScope(SportsMarketScopeType.UNSUPPORTED_PERIOD)
    if "set total" in text or "set totals" in text or "total sets" in text:
        return SportsMarketScope(SportsMarketScopeType.TENNIS_TOTAL_SETS)
    if "match total" in text or "total games" in text:
        if _is_tennis_scope_candidate(market):
            return SportsMarketScope(SportsMarketScopeType.TENNIS_MATCH_GAMES)
        return SportsMarketScope(SportsMarketScopeType.FULL_GAME)
    if _is_tennis_scope_candidate(market):
        return SportsMarketScope(SportsMarketScopeType.TENNIS_MATCH_GAMES)
    return SportsMarketScope(SportsMarketScopeType.FULL_GAME)


def _tennis_set_games_total_number(text: str) -> int | None:
    """识别第一盘/第二盘等单盘总局数盘口。"""

    set_markers = (
        (1, ("first set total", "1st set total", "set 1 total")),
        (2, ("second set total", "2nd set total", "set 2 total")),
        (3, ("third set total", "3rd set total", "set 3 total")),
        (4, ("fourth set total", "4th set total", "set 4 total")),
        (5, ("fifth set total", "5th set total", "set 5 total")),
    )
    for set_number, markers in set_markers:
        if any(marker in text for marker in markers):
            return set_number
    return None


def _is_unsupported_period_total(text: str) -> bool:
    """识别当前没有独立直播字段支撑的分段 totals。"""

    period_markers = (
        "first quarter total",
        "1st quarter total",
        "second quarter total",
        "2nd quarter total",
        "third quarter total",
        "3rd quarter total",
        "fourth quarter total",
        "4th quarter total",
        "first half total",
        "1st half total",
        "second half total",
        "2nd half total",
        "first inning total",
        "1st inning total",
        "first 5 innings total",
        "first five innings total",
    )
    return any(marker in text for marker in period_markers)


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


def _mlb_eighth_moneyline_lead_reached(
    game: LiveGameState,
    side: SportsMarketSide,
    policy: SportsTailPolicy,
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
    policy: SportsTailPolicy,
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
        first_to_serve=None if value.get("first_to_serve") is None else str(value.get("first_to_serve")),
        serving_side=None if value.get("serving_side") is None else str(value.get("serving_side")),
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
