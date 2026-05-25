"""体育策略类型集合包。

历史上承载扫尾评估器（OUTCOME_NOT_LOCKED / sport-specific dispatch），
现已删除——「门禁过了即进场」哲学下，进场判断只剩硬约束（live source / cash /
best_ask / state），进场后行为全部走 position_plan。

本包只保留：
- 通用体育类型（re-export from sports_framework）
- 策略层基础类型（TailPolicy / TailAction / ExecutionPermission，仅供 Kelly /
  exit overlay 等下游消费）
"""

from __future__ import annotations

from polymarket_trader.domain.sports_live import BaseballGameState
from polymarket_trader.sports import (
    LiveGameState,
    LiveGameStatus,
    SportsMarketFamily,
    SportsMarketScope,
    SportsMarketScopeType,
    SportsMarketSide,
    SportsMarketSnapshot,
    SportsMarketType,
    TennisGameState,
    live_game_state_from_metadata,
)

from .types import (
    ExecutionPermission,
    SportsTailCandidate,
    TailAction,
    TailEvaluation,
    TailPolicy,
)

__all__ = [
    "BaseballGameState",
    "ExecutionPermission",
    "LiveGameState",
    "LiveGameStatus",
    "SportsMarketFamily",
    "SportsMarketScope",
    "SportsMarketScopeType",
    "SportsMarketSide",
    "SportsMarketSnapshot",
    "SportsMarketType",
    "SportsTailCandidate",
    "TailAction",
    "TailEvaluation",
    "TailPolicy",
    "TennisGameState",
    "live_game_state_from_metadata",
]
