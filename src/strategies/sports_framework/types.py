"""体育策略通用类型层：枚举 + 内部 dataclass。

不依赖任何评估或解析逻辑；任何体育策略都可以从这里取业务对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from polymarket_trader.domain.sports_live import (
    BaseballGameState,
    BasketballGameState,
    EsportsGameState,
    SoccerGameState,
    TennisGameState,
    CricketGameState,
    VolleyballGameState,
)


class SportsMarketType(StrEnum):
    """体育盘口类型。"""

    TOTALS = "totals"
    MONEYLINE = "moneyline"
    SPREADS = "spreads"
    BINARY_PROP = "binary_prop"
    PLAYER_PROP = "player_prop"


class SportsMarketFamily(StrEnum):
    """按结算语义划分的市场家族。"""

    SINGLE_GAME = "single_game"
    SERIES = "series"
    OUTRIGHT = "outright"
    ESPORTS = "esports"
    UNSUPPORTED = "unsupported"


class SportsMarketSide(StrEnum):
    """体育盘口方向。"""

    OVER = "over"
    UNDER = "under"
    HOME = "home"
    AWAY = "away"
    YES = "yes"
    NO = "no"


class SportsMarketScopeType(StrEnum):
    """盘口结算范围。"""

    FULL_GAME = "full_game"
    TENNIS_MATCH_GAMES = "tennis_match_games"
    TENNIS_TOTAL_SETS = "tennis_total_sets"
    TENNIS_SET_GAMES = "tennis_set_games"
    # 篮球上半场盘口（1H total / 1H spread / 1H moneyline）。
    BASKETBALL_FIRST_HALF = "basketball_first_half"
    UNSUPPORTED_PERIOD = "unsupported_period"


class LiveGameStatus(StrEnum):
    """策略侧直播比赛状态。"""

    SCHEDULED = "scheduled"
    LIVE = "live"
    PAUSED = "paused"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"
    RETIRED = "retired"
    ENDED = "ended"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LiveGameState:
    """策略评估所需的直播比赛状态。

    所有 sport-specific state（baseball/tennis/soccer/esports/cricket/volleyball）均复用
    ``polymarket_trader.domain.sports_live`` 中的单一定义，与 infra 归一化保持
    类型同源。
    """

    league: str
    home_name: str
    away_name: str
    home_score: int
    away_score: int
    period: str
    status: LiveGameStatus
    seconds_remaining: int | None = None
    observed_at: datetime | None = None
    source_conflicts: tuple[Mapping[str, Any], ...] = ()
    sport: str = ""
    baseball_state: BaseballGameState | None = None
    basketball_state: BasketballGameState | None = None
    tennis_state: TennisGameState | None = None
    soccer_state: SoccerGameState | None = None
    esports_state: EsportsGameState | None = None
    cricket_state: CricketGameState | None = None
    volleyball_state: VolleyballGameState | None = None

    @property
    def total_score(self) -> int:
        return self.home_score + self.away_score

    def score_diff_for(self, side: SportsMarketSide) -> int:
        if side == SportsMarketSide.HOME:
            return self.home_score - self.away_score
        if side == SportsMarketSide.AWAY:
            return self.away_score - self.home_score
        return 0


@dataclass(frozen=True, slots=True)
class SportsMarketSnapshot:
    """策略评估所需的体育盘口快照。"""

    market_type: SportsMarketType
    side: SportsMarketSide
    token_id: str
    line: Decimal | None
    best_ask: Decimal | None
    buyable_liquidity_usdc: Decimal
    market_family: SportsMarketFamily = SportsMarketFamily.SINGLE_GAME
    market_slug: str | None = None
    market_end_date: datetime | None = None
    scope_type: SportsMarketScopeType = SportsMarketScopeType.FULL_GAME
    scope_number: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    best_bid: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SportsMarketScope:
    """结构化盘口结算范围。"""

    scope_type: SportsMarketScopeType
    scope_number: int | None = None
