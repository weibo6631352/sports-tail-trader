from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from polymarket_trader.serialization import jsonable


class SportsLiveGameStatus(StrEnum):
    """外部体育比分源归一后的比赛状态。"""

    SCHEDULED = "scheduled"
    LIVE = "live"
    PAUSED = "paused"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"
    RETIRED = "retired"
    ENDED = "ended"
    UNKNOWN = "unknown"


class SportsLiveSourceHealth(StrEnum):
    """单个外部直播源在一次同步中的健康语义。"""

    SUCCESS_WITH_LIVE_DATA = "success_with_live_data"
    SUCCESS_EMPTY = "success_empty"
    CACHED = "cached"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"
    COOLDOWN = "cooldown"
    EVICTED = "evicted"


@dataclass(frozen=True, slots=True)
class SportsLiveTeam:
    """外部比分源中的队伍或选手信息。"""

    name: str
    score: int
    display_name: str | None = None
    abbreviation: str | None = None
    short_name: str | None = None
    location: str | None = None
    aliases: tuple[str, ...] = ()

    def match_aliases(self) -> tuple[str, ...]:
        aliases = [
            self.name,
            self.display_name,
            self.abbreviation,
            self.short_name,
            self.location,
            *self.aliases,
        ]
        if self.location and self.name:
            aliases.append(f"{self.location} {self.name}")
        seen: set[str] = set()
        result: list[str] = []
        for alias in aliases:
            if alias is None:
                continue
            text = str(alias).strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            result.append(text)
        return tuple(result)


@dataclass(frozen=True, slots=True)
class BaseballGameState:
    """棒球比赛当前局面。"""

    current_inning: int | None = None
    inning_half: str | None = None
    outs: int | None = None
    offense_team: str | None = None
    defense_team: str | None = None
    occupied_bases: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TennisGameState:
    """网球比赛盘分、局分和即时分状态。

    域层单一类型源，infra 直播源和策略层共享同一份字段定义。
    """

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

    def sets_won_for(self, side: Any) -> int:
        key = getattr(side, "value", side)
        if key == "home":
            return self.home_sets_won
        if key == "away":
            return self.away_sets_won
        return 0

    def current_set_games_for(self, side: Any) -> int | None:
        key = getattr(side, "value", side)
        if key == "home":
            return self.home_current_set_games
        if key == "away":
            return self.away_current_set_games
        return None


@dataclass(frozen=True, slots=True)
class SoccerGameState:
    """足球（含美式 soccer）比赛节奏状态。

    period 取值范围："first_half" / "second_half" / "extra_time" / "penalties"；
    clock_minutes 是已进行的本节内分钟（不含补时）。
    """

    period: str | None = None
    clock_minutes: int | None = None
    added_minutes: int | None = None
    home_red_cards: int = 0
    away_red_cards: int = 0
    home_yellow_cards: int = 0
    away_yellow_cards: int = 0
    last_event_minute: int | None = None


@dataclass(frozen=True, slots=True)
class EsportsGameState:
    """电子竞技 best-of-N 系列赛状态。

    best_of 是本场总图数（BO3/BO5/BO7），current_map_index 是当前图序号（从 1 起），
    map_score 是当前图比分（如 CS2 回合数）。
    """

    best_of: int | None = None
    current_map_index: int | None = None
    home_maps_won: int = 0
    away_maps_won: int = 0
    home_current_map_score: int | None = None
    away_current_map_score: int | None = None
    map_winners: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CricketGameState:
    """板球比赛局面。

    板球以 ``over`` 为投球单位，每个 over 含 6 个合法球。这里用整数
    ``overs_completed`` + ``balls_in_over`` 分别表示已结束 overs 数和当前
    over 内已投的合法球数，避免浮点 12.3 表示带来的精度问题。
    ``target`` 仅在追分（chase）阶段有意义。
    """

    current_innings: int | None = None
    batting_side: str | None = None
    runs: int | None = None
    wickets: int | None = None
    overs_completed: int | None = None
    balls_in_over: int | None = None
    target: int | None = None
    required_runs: int | None = None
    required_balls: int | None = None


@dataclass(frozen=True, slots=True)
class SportsLiveRaceEvent:
    """赛车/竞速类比赛实时状态（不是 team-pair，独立形态）。

    与 SportsLiveGame 并存：F1/NASCAR/IndyCar/MotoGP 不能映射为 home/away。
    """

    source: str
    source_event_id: str
    league: str
    event_name: str
    status: SportsLiveGameStatus
    leader_driver: str | None = None
    leader_team: str | None = None
    laps_completed: int | None = None
    total_laps: int | None = None
    status_flag: str | None = None
    observed_at: datetime | None = None
    raw_status: str | None = None
    drivers: tuple["SportsLiveDriverPosition", ...] = ()
    source_payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SportsLiveDriverPosition:
    """赛车比赛中单个车手的瞬时位次。"""

    driver: str
    position: int | None = None
    team: str | None = None
    laps_completed: int | None = None
    gap_to_leader: str | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class SportsLiveGame:
    """策略入场前需要的归一化直播比赛状态。"""

    source: str
    source_event_id: str
    league: str
    home: SportsLiveTeam
    away: SportsLiveTeam
    status: SportsLiveGameStatus
    period: str
    seconds_remaining: int | None = None
    observed_at: datetime | None = None
    raw_status: str | None = None
    baseball_state: BaseballGameState | None = None
    tennis_state: TennisGameState | None = None
    soccer_state: SoccerGameState | None = None
    esports_state: EsportsGameState | None = None
    cricket_state: CricketGameState | None = None
    source_payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def total_score(self) -> int:
        return self.home.score + self.away.score

    def as_payload(self) -> dict[str, Any]:
        """返回不含外部原始 payload 的可审计内部快照。"""

        return {
            "source": self.source,
            "source_event_id": self.source_event_id,
            "league": self.league,
            "home": jsonable(self.home),
            "away": jsonable(self.away),
            "status": self.status.value,
            "period": self.period,
            "seconds_remaining": self.seconds_remaining,
            "observed_at": None if self.observed_at is None else self.observed_at.isoformat(),
            "raw_status": self.raw_status,
            "baseball_state": None if self.baseball_state is None else jsonable(self.baseball_state),
            "tennis_state": None if self.tennis_state is None else jsonable(self.tennis_state),
            "soccer_state": None if self.soccer_state is None else jsonable(self.soccer_state),
            "esports_state": None if self.esports_state is None else jsonable(self.esports_state),
            "cricket_state": None if self.cricket_state is None else jsonable(self.cricket_state),
        }


@dataclass(frozen=True, slots=True)
class SportsLiveSourceStatus:
    """一次聚合同步中单个外部源的状态摘要。"""

    source: str
    success: bool
    health: SportsLiveSourceHealth = SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
    games_seen: int = 0
    observed_at: datetime | None = None
    last_error: str | None = None
    cooldown_until: datetime | None = None
    consecutive_failures: int = 0

    def as_dict(self) -> dict[str, Any]:
        """返回可序列化的来源状态。"""

        return jsonable(self)


@dataclass(frozen=True, slots=True)
class SportsLiveSnapshot:
    """一次外部比分源拉取后的归一化比赛集合。"""

    source: str
    observed_at: datetime
    games: tuple[SportsLiveGame, ...]
    source_statuses: tuple[SportsLiveSourceStatus, ...] = ()
    race_events: tuple[SportsLiveRaceEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class SportsLiveSyncStatus:
    """外部直播状态同步器的可观测快照。"""

    enabled: bool
    source: str
    running: bool = False
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_games_seen: int = 0
    last_markets_seen: int = 0
    last_matches: int = 0
    last_records_written: int = 0
    last_unmatched_markets: int = 0
    last_entry_signals_published: int = 0
    leagues: tuple[str, ...] = ()
    source_statuses: tuple[SportsLiveSourceStatus, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)
