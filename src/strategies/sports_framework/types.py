"""体育策略通用类型层：枚举 + 内部 dataclass。

不依赖任何评估或解析逻辑；任何体育策略都可以从这里取业务对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from polymarket_trader.domain.sports_live import BaseballGameState


class SportsMarketType(StrEnum):
    """体育盘口类型。"""

    TOTALS = "totals"
    MONEYLINE = "moneyline"
    SPREADS = "spreads"
    BINARY_PROP = "binary_prop"


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
class TennisGameState:
    """策略评估网球扫尾所需的盘分、局分和即时分状态。"""

    home_sets_won: int = 0
    away_sets_won: int = 0
    current_set: int | None = None
    home_current_set_games: int | None = None
    away_current_set_games: int | None = None
    home_total_games: int = 0
    away_total_games: int = 0
    set_scores: tuple[tuple[int, int], ...] = ()
    home_point: str | None = None
    away_point: str | None = None
    first_to_serve: str | None = None
    serving_side: str | None = None

    @property
    def total_games(self) -> int:
        return self.home_total_games + self.away_total_games

    def sets_won_for(self, side: SportsMarketSide) -> int:
        if side == SportsMarketSide.HOME:
            return self.home_sets_won
        if side == SportsMarketSide.AWAY:
            return self.away_sets_won
        return 0

    def current_set_games_for(self, side: SportsMarketSide) -> int | None:
        if side == SportsMarketSide.HOME:
            return self.home_current_set_games
        if side == SportsMarketSide.AWAY:
            return self.away_current_set_games
        return None


@dataclass(frozen=True, slots=True)
class LiveGameState:
    """策略评估所需的直播比赛状态。

    ``baseball_state`` 复用 ``polymarket_trader.domain.sports_live.BaseballGameState``，
    与 infra 直播源归一化保持单一类型源；``tennis_state`` 暂未被 infra 归一化，
    保留在策略通用层。
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
    baseball_state: BaseballGameState | None = None
    tennis_state: TennisGameState | None = None

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


@dataclass(frozen=True, slots=True)
class SportsMarketScope:
    """结构化盘口结算范围。"""

    scope_type: SportsMarketScopeType
    scope_number: int | None = None
