"""体育通用类型层。

任何 workflow 都可以复用的能力：
- 直播比赛状态归一化（LiveGameState / TennisGameState 与外部 metadata 的解析）
- 联赛识别（MLB / NFL / Tennis）
- 体育盘口结构化建模（SportsMarketSnapshot / SportsMarketFamily / SportsMarketScope）
- Market slug 通用解析（结算范围识别）

workflow 层专属语义（profit-take 阈值 / execution permission 等）不在本包，
留在 ``polymarket_trader.workflow`` 子模块；本包不反向依赖任何具体 workflow 包。
"""

from __future__ import annotations

from polymarket_trader.domain.sports_live import (
    BaseballGameState,
    CricketGameState,
    EsportsGameState,
    HandballGameState,
    SoccerGameState,
    TennisGameState,
)

from .leagues import (
    is_combat_sport_game,
    is_cricket_game,
    is_handball_game,
    is_hockey_game,
    is_mlb_game,
    is_nfl_game,
    is_soccer_game,
    is_tennis_game,
)
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
)

__all__ = [
    "BaseballGameState",
    "CricketGameState",
    "EsportsGameState",
    "HandballGameState",
    "LiveGameState",
    "LiveGameStatus",
    "SoccerGameState",
    "SportsMarketFamily",
    "SportsMarketScope",
    "SportsMarketScopeType",
    "SportsMarketSide",
    "SportsMarketSnapshot",
    "SportsMarketType",
    "TennisGameState",
    "is_combat_sport_game",
    "is_cricket_game",
    "is_handball_game",
    "is_hockey_game",
    "is_mlb_game",
    "is_nfl_game",
    "is_soccer_game",
    "is_tennis_game",
    "is_tennis_scope_candidate",
    "is_unsupported_period_total",
    "live_game_state_from_metadata",
    "market_scope",
    "normalized_market_slug",
    "totals_market_scope",
]
