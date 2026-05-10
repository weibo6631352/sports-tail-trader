"""体育扫尾策略的纯类型层：枚举 + 内部 dataclass。

不依赖任何评估或解析逻辑；所有评估函数最终都从这里取业务对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
class TailPolicy:
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
class TailEvaluation:
    """统一体育扫尾评估结果。"""

    accepted: bool
    action: TailAction
    reason: str
    candidate: SportsTailCandidate | None = None
    execution_permission: ExecutionPermission | None = None
    opportunity_type: SportsTailOpportunityType = SportsTailOpportunityType.LIVE_TAIL
    metadata: Mapping[str, Any] = field(default_factory=dict)
