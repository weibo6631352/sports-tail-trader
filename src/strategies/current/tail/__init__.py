"""体育直播扫尾策略包。

拆分为 types/parsing/leagues/slug/core/mlb/tennis/evaluator 等子模块；
``__init__`` 统一对外暴露公开类型和评估入口，调用方按
``from strategies.current.tail import X`` 使用即可。
"""

from __future__ import annotations

from .evaluator import (
    evaluate_scale_in_opportunity,
    evaluate_tail_opportunity,
)
from .parsing import live_game_state_from_metadata
from .types import (
    BaseballGameState,
    ExecutionPermission,
    LiveGameState,
    LiveGameStatus,
    SportsMarketFamily,
    SportsMarketScope,
    SportsMarketScopeType,
    SportsMarketSide,
    SportsMarketSnapshot,
    SportsMarketType,
    SportsTailCandidate,
    TailEvaluation,
    SportsTailOpportunityType,
    TailPolicy,
    TailAction,
    TailRejectReason,
    TennisGameState,
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
    "TailEvaluation",
    "SportsTailOpportunityType",
    "TailPolicy",
    "TailAction",
    "TailRejectReason",
    "TennisGameState",
    "evaluate_scale_in_opportunity",
    "evaluate_tail_opportunity",
    "live_game_state_from_metadata",
]
