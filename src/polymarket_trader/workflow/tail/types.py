"""策略层基础类型。

历史上承载完整扫尾评估器（TailRejectReason / SportsTailOpportunityType /
sport-specific dispatch），现已删除——「门禁过了即进场」哲学下，进场判断只剩硬约束，
所有进场后行为走 position_plan。

本模块只保留下游（Kelly sizing / Goalserve 调参 / exit overlay metadata）仍需
消费的策略基础类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from polymarket_trader.sports import (
    LiveGameState,
    SportsMarketSnapshot,
    SportsMarketType,
)


class ExecutionPermission(StrEnum):
    """执行权限——历史评估器输出，目前仅由 outright / series 子策略使用。"""

    RECORD_ONLY = "record_only"
    ALERT_ONLY = "alert_only"
    MANUAL_CONFIRM = "manual_confirm"
    AUTO_EXECUTE = "auto_execute"


class TailAction(StrEnum):
    """历史评估器建议动作——目前仅 outright / audit metadata 使用。"""

    REJECT = "reject"
    RECORD = "record"
    ALERT = "alert"
    MANUAL_CONFIRM = "manual_confirm"
    AUTO_EXECUTE = "auto_execute"


@dataclass(frozen=True, slots=True)
class TailPolicy:
    """Goalserve 等下游调参仍读这些字段；进场决策不再用。"""

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
    soccer_max_game_state_age_seconds: int = 120
    esports_max_game_state_age_seconds: int = 90
    max_under_seconds_remaining: int = 30
    min_under_safety_margin: Decimal = Decimal("2")
    mlb_under_min_inning: int = 6
    mlb_under_inning_margin_step: Decimal = Decimal("2")
    min_moneyline_lead: int = 6
    soccer_min_moneyline_lead: int = 1
    hockey_min_moneyline_lead: int = 1
    mlb_eighth_moneyline_min_lead: int = 2
    mlb_ninth_moneyline_min_lead: int = 3
    min_spread_safety_margin: Decimal = Decimal("2")


@dataclass(frozen=True, slots=True)
class SportsTailCandidate:
    """候选数据载体；目前仅 audit metadata / outright path 使用。"""

    game: LiveGameState
    market: SportsMarketSnapshot
    reason: str
    risk_notes: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TailEvaluation:
    """评估结果载体；进场路径已不再产出，仅 outright path 仍构造。"""

    accepted: bool
    action: TailAction
    reason: str
    candidate: SportsTailCandidate | None = None
    execution_permission: ExecutionPermission | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
