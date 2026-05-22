"""体育扫尾策略私有类型层：策略评估、政策与决策结果。

通用体育类型（``LiveGameState`` / ``SportsMarketSnapshot`` / ``BaseballGameState`` /
``TennisGameState`` 等）已上提到 ``strategies.sports_framework``；本模块只承载
与扫尾语义强绑定的类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from strategies.sports_framework import (
    LiveGameState,
    SportsMarketSnapshot,
    SportsMarketType,
)


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
    PRICE_BELOW_MIN = "price_below_min"
    LIQUIDITY_BELOW_MIN = "liquidity_below_min"
    OUTCOME_NOT_LOCKED = "outcome_not_locked"
    MISSING_SECONDS_REMAINING = "missing_seconds_remaining"
    GAME_NOT_LATE_ENOUGH = "game_not_late_enough"
    INSUFFICIENT_LEAD = "insufficient_lead"
    INSUFFICIENT_SAFETY_MARGIN = "insufficient_safety_margin"
    UNSUPPORTED_MARKET_TYPE = "unsupported_market_type"
    UNSUPPORTED_MARKET_SIDE = "unsupported_market_side"
    UNSUPPORTED_MARKET_SCOPE = "unsupported_market_scope"
    OUTRIGHT_MARKET_NOT_AUTO_TRADABLE = "outright_market_not_auto_tradable"
    ESPORTS_MARKET_NOT_AUTO_TRADABLE = "esports_market_not_auto_tradable"
    UNSUPPORTED_MARKET_FAMILY = "unsupported_market_family"
    LIVE_SOURCE_CONFLICT = "live_source_conflict"
    MISSING_BASEBALL_STATE = "missing_baseball_state"
    BASEBALL_NOT_LATE_ENOUGH = "baseball_not_late_enough"
    BASEBALL_THREAT_ON_BASE = "baseball_threat_on_base"
    BASEBALL_OFFENSE_NOT_TRAILING = "baseball_offense_not_trailing"
    BASEBALL_FIRST_INNING_NOT_COMPLETE = "baseball_first_inning_not_complete"
    MISSING_BASKETBALL_STATE = "missing_basketball_state"
    BASKETBALL_FIRST_HALF_NOT_COMPLETE = "basketball_first_half_not_complete"
    SOCCER_HALFTIME_NOT_COMPLETE = "soccer_halftime_not_complete"
    MISSING_TENNIS_STATE = "missing_tennis_state"
    TENNIS_NOT_LATE_ENOUGH = "tennis_not_late_enough"
    TENNIS_TOTALS_UNDER_NOT_SUPPORTED = "tennis_totals_under_not_supported"
    TENNIS_SET_WINNER_NOT_SUPPORTED = "tennis_set_winner_not_supported"
    TENNIS_TOTAL_SCOPE_UNSUPPORTED = "tennis_total_scope_unsupported"
    TENNIS_SPREADS_NOT_SUPPORTED = "tennis_spreads_not_supported"
    RUGBY_NOT_LATE_ENOUGH = "rugby_not_late_enough"
    RUGBY_LEAD_NOT_SAFE = "rugby_lead_not_safe"
    RUGBY_DRAW_NOT_SUPPORTED = "rugby_draw_not_supported"
    RUGBY_MARKET_NOT_SUPPORTED = "rugby_market_not_supported"
    MISSING_ESPORTS_STATE = "missing_esports_state"
    ESPORTS_BEST_OF_UNKNOWN = "esports_best_of_unknown"


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
    min_entry_price: Decimal = Decimal("0.10")
    totals_max_entry_price: Decimal = Decimal("0.99")
    moneyline_max_entry_price: Decimal = Decimal("0.97")
    locked_outcome_max_entry_price: Decimal = Decimal("0.98")
    spreads_max_entry_price: Decimal = Decimal("0.96")
    min_liquidity_usdc: Decimal = Decimal("1")
    max_game_state_age_seconds: int = 10
    baseball_max_game_state_age_seconds: int = 45
    tennis_max_game_state_age_seconds: int = 35
    max_under_seconds_remaining: int = 30
    max_moneyline_seconds_remaining: int = 180
    max_spreads_seconds_remaining: int = 120
    min_under_safety_margin: Decimal = Decimal("2")
    # MLB Under 总分入场最早可考虑的局数；早于此局一律拒绝。
    mlb_under_min_inning: int = 6
    # MLB Under 每提前一局（早于 9 局）额外要求的 safety margin。
    # 默认 9 局基准 margin=2，每往前一局 +2：8 局需 4、7 局需 6、6 局需 8。
    # margin 足够大时 Under 在该局已基本锁定，配合提前止盈做准量化盈利。
    mlb_under_inning_margin_step: Decimal = Decimal("2")
    min_moneyline_lead: int = 6
    soccer_min_moneyline_lead: int = 1
    hockey_min_moneyline_lead: int = 1
    mlb_eighth_moneyline_min_lead: int = 2
    mlb_ninth_moneyline_min_lead: int = 3
    min_spread_safety_margin: Decimal = Decimal("2")


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
