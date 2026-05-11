"""体育直播扫尾策略包。

通用体育能力（LiveGameState / SportsMarket* / 联赛识别 / 通用 slug 解析）已上提到
``strategies.sports_framework``；本包内只承载扫尾专属语义（TailPolicy /
TailEvaluation / 评估器分派 / MLB / Tennis 私有规则）。

为保持外部调用方零改动，``__init__`` 同时 re-export 通用与专属符号；
``from strategies.current.tail import X`` 仍按以前用法即可。
"""

from __future__ import annotations

from polymarket_trader.domain.sports_live import BaseballGameState
from strategies.sports_framework import (
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

from .evaluator import (
    evaluate_scale_in_opportunity,
    evaluate_tail_opportunity,
)
from .types import (
    ExecutionPermission,
    SportsTailCandidate,
    SportsTailOpportunityType,
    TailAction,
    TailEvaluation,
    TailPolicy,
    TailRejectReason,
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
    "SportsTailOpportunityType",
    "TailAction",
    "TailEvaluation",
    "TailPolicy",
    "TailRejectReason",
    "TennisGameState",
    "evaluate_scale_in_opportunity",
    "evaluate_tail_opportunity",
    "live_game_state_from_metadata",
]
