"""Outright 评估类型层。

与 ``tail/types.py`` 平行：outright 不依赖 LiveGameState / SportsMarketSnapshot
（那是单场 family 专有），而是依赖 SeasonOddsSnapshot 与 OrderbookView。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from strategies.current.tail.types import ExecutionPermission, TailAction


class OutrightAction(StrEnum):
    """outright 评估后的建议动作。沿用 TailAction 字面值便于 framework 透明传递。"""

    REJECT = "reject"
    RECORD = "record"
    AUTO_EXECUTE = "auto_execute"
    ALERT = "alert"
    MANUAL_CONFIRM = "manual_confirm"


class OutrightRejectReason(StrEnum):
    """outright 评估拒绝原因。每个值都可审计、对齐 CLAUDE.md §9。"""

    MISSING_SEASON_ODDS = "missing_season_odds"
    STALE_SEASON_ODDS = "stale_season_odds"
    ODDS_OUTCOME_NOT_MAPPED = "odds_outcome_not_mapped"
    OUTRIGHT_TEAM_NOT_RESOLVED = "outright_team_not_resolved"
    SEASON_ODDS_INCOMPLETE = "season_odds_incomplete"
    INSUFFICIENT_EDGE = "insufficient_edge"
    PRICE_ABOVE_FAIR = "price_above_fair"
    MISSING_BEST_ASK = "missing_best_ask"
    LIQUIDITY_BELOW_MIN = "liquidity_below_min"
    OUTCOME_RESOLVED = "outcome_resolved"
    HOLD_HORIZON_EXCEEDED = "hold_horizon_exceeded"
    MIN_REMAINING_DAYS_NOT_MET = "min_remaining_days_not_met"
    EVENT_CORRELATION_CAP = "event_correlation_cap"
    TOTAL_BUDGET_EXHAUSTED = "total_budget_exhausted"
    PER_MARKET_CAP_EXCEEDED = "per_market_cap_exceeded"
    MARKET_END_PASSED = "market_end_passed"
    SOURCE_CONFLICT = "source_conflict"


@dataclass(frozen=True, slots=True)
class OutrightCandidate:
    """outright 评估候选。"""

    market_slug: str
    condition_id: str
    outcome_label: str
    token_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutrightEvaluation:
    """outright 评估结果。"""

    accepted: bool
    action: OutrightAction
    reason: str
    candidate: OutrightCandidate | None = None
    execution_permission: ExecutionPermission | None = None
    fair_value: Decimal | None = None
    entry_price_cap: Decimal | None = None
    exit_price_target: Decimal | None = None
    reject_reason: OutrightRejectReason | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


def outright_action_for_permission(permission: ExecutionPermission) -> OutrightAction:
    """把执行权限映射到 outright 动作。与 tail/core._action_for_permission 同语义。"""

    if permission == ExecutionPermission.RECORD_ONLY:
        return OutrightAction.RECORD
    if permission == ExecutionPermission.ALERT_ONLY:
        return OutrightAction.ALERT
    if permission == ExecutionPermission.MANUAL_CONFIRM:
        return OutrightAction.MANUAL_CONFIRM
    return OutrightAction.AUTO_EXECUTE


# 同侧映射 TailAction，方便 framework 把 outright 决策转成 ExtensionDecision 时
# 用同一份枚举做下游 metadata。
_OUTRIGHT_TO_TAIL_ACTION: dict[OutrightAction, TailAction] = {
    OutrightAction.REJECT: TailAction.REJECT,
    OutrightAction.RECORD: TailAction.RECORD,
    OutrightAction.AUTO_EXECUTE: TailAction.AUTO_EXECUTE,
    OutrightAction.ALERT: TailAction.ALERT,
    OutrightAction.MANUAL_CONFIRM: TailAction.MANUAL_CONFIRM,
}


def to_tail_action(action: OutrightAction) -> TailAction:
    return _OUTRIGHT_TO_TAIL_ACTION.get(action, TailAction.REJECT)
