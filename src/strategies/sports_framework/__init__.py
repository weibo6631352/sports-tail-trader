"""体育策略通用层。

任何体育策略都可以复用的能力：
- 直播比赛状态归一化（LiveGameState / TennisGameState 与外部 metadata 的解析）
- 联赛识别（MLB / NFL / Tennis）
- 体育盘口结构化建模（SportsMarketSnapshot / SportsMarketFamily / SportsMarketScope）
- Market slug 通用解析（结算范围识别）

扫尾策略专属语义（TailPolicy / TailEvaluation / profit-take 阈值 / TailRejectReason 等）
不在本包，留在 ``strategies.current.tail``；本包不反向依赖任何具体策略包。
"""

from __future__ import annotations

from polymarket_trader.domain.sports_live import BaseballGameState

from .leagues import is_mlb_game, is_nfl_game, is_tennis_game
from .parsing import live_game_state_from_metadata
from .slug import (
    is_tennis_scope_candidate,
    is_unsupported_period_total,
    market_scope,
    normalized_market_slug,
    totals_market_scope,
)
from .types import (
    LiveGameState,
    LiveGameStatus,
    SportsMarketFamily,
    SportsMarketScope,
    SportsMarketScopeType,
    SportsMarketSide,
    SportsMarketSnapshot,
    SportsMarketType,
    TennisGameState,
)

__all__ = [
    "BaseballGameState",
    "LiveGameState",
    "LiveGameStatus",
    "SportsMarketFamily",
    "SportsMarketScope",
    "SportsMarketScopeType",
    "SportsMarketSide",
    "SportsMarketSnapshot",
    "SportsMarketType",
    "TennisGameState",
    "is_mlb_game",
    "is_nfl_game",
    "is_tennis_game",
    "is_tennis_scope_candidate",
    "is_unsupported_period_total",
    "live_game_state_from_metadata",
    "market_scope",
    "normalized_market_slug",
    "totals_market_scope",
]
