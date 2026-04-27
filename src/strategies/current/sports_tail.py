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


class SportsMarketSide(StrEnum):
    """体育盘口方向。"""

    OVER = "over"
    UNDER = "under"
    HOME = "home"
    AWAY = "away"


class LiveGameStatus(StrEnum):
    """策略侧直播比赛状态。"""

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
    min_liquidity_usdc: Decimal = Decimal("5")
    max_game_state_age_seconds: int = 10
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
    )


def evaluate_tail_opportunity(
    game: LiveGameState | None,
    market: SportsMarketSnapshot,
    *,
    policy: SportsTailPolicy,
    now: datetime | None = None,
) -> SportsTailEvaluation:
    """评估体育盘口是否构成扫尾机会。"""

    if game is None:
        return _reject(None, TailRejectReason.MISSING_LIVE_GAME_STATE.value)

    candidate = _candidate(game, market)
    common_reject_reason = _common_reject_reason(game, market, policy, now=now)
    if common_reject_reason:
        return _reject(candidate, common_reject_reason.value)

    if market.market_type == SportsMarketType.TOTALS:
        return _evaluate_totals(candidate, policy)
    if market.market_type == SportsMarketType.MONEYLINE:
        return _evaluate_moneyline(candidate, policy)
    if market.market_type == SportsMarketType.SPREADS:
        return _evaluate_spreads(candidate, policy)
    return _reject(candidate, TailRejectReason.UNSUPPORTED_MARKET_TYPE.value)


def _candidate(game: LiveGameState, market: SportsMarketSnapshot) -> SportsTailCandidate:
    return SportsTailCandidate(
        game=game,
        market=market,
        reason="sports_tail_candidate",
        metadata={
            "market_type": market.market_type.value,
            "side": market.side.value,
            "line": str(market.line) if market.line is not None else None,
            "best_ask": str(market.best_ask) if market.best_ask is not None else None,
            "total_score": game.total_score,
            "seconds_remaining": game.seconds_remaining,
            "game_status": game.status.value,
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
    if _is_stale(game, policy, now=now):
        return TailRejectReason.STALE_GAME_STATE
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


def _accept(
    candidate: SportsTailCandidate,
    reason: str,
    permission: ExecutionPermission,
) -> SportsTailEvaluation:
    return SportsTailEvaluation(
        accepted=True,
        action=_action_for_permission(permission),
        reason=reason,
        candidate=candidate,
        execution_permission=permission,
        metadata=candidate.metadata,
    )


def _reject(
    candidate: SportsTailCandidate | None,
    reason: str,
) -> SportsTailEvaluation:
    return SportsTailEvaluation(
        accepted=False,
        action=TailAction.REJECT,
        reason=reason,
        candidate=candidate,
        execution_permission=None,
        metadata={} if candidate is None else candidate.metadata,
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
    return age_seconds > policy.max_game_state_age_seconds


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


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
