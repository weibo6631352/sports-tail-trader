"""体育策略类型集合包。

历史扫尾评估器（TailPolicy / TailEvaluation / SportsTailCandidate / TailAction）
已删除——「门禁过了即进场」量化哲学下，进场判断只剩硬约束（live source / cash /
best_ask / state），进场后走 position_plan + 动态退出引擎。

本包只保留：
- 通用体育类型（re-export from sports）
- ``ExecutionPermission`` 枚举（outright / series family 权限）
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

from .types import ExecutionPermission

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
    "TennisGameState",
    "live_game_state_from_metadata",
]
