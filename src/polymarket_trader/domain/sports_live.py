"""体育直播状态的归一化 domain 模型。

LiveEvent 统一覆盖 team_match（NBA/网球/电竞，含 home/away）与 race
（F1/NASCAR/MotoGP，N 个 driver 参与）与 tournament_field（高尔夫等多人锦标赛）。
赛车不是 home/away，按 ``kind=RACE`` 走 N 个 ``Participant(role="driver")``；
匹配链路按 ``kind`` 分支，避免在 dedup/audit/admin 三处各写一遍 team/race 双形态。

聚合融合后，``source`` 记录主源、``contributing_sources`` 记录所有贡献源、
``source_conflicts`` 记录被压制的字段（一等字段，不藏在 source_payload 字典里
做 duck-typing fallback）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from polymarket_trader.serialization import jsonable


class LiveEventKind(StrEnum):
    """LiveEvent 的形态分类。

    TEAM_MATCH：双方对抗（NBA/MLB/NHL/网球/电竞 BO 系列），有 home/away 概念。
    RACE：赛车/竞速，N 个 driver，按 position 排位，不存在 home/away。
    TOURNAMENT_FIELD：高尔夫等多人锦标赛 leaderboard，N 个 player + position。
    """

    TEAM_MATCH = "team_match"
    RACE = "race"
    TOURNAMENT_FIELD = "tournament_field"


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
class Participant:
    """LiveEvent 的参与方（队伍 / 车手 / 选手）。

    ``role`` 由 ``LiveEventKind`` 决定语义：
      - TEAM_MATCH: "home" / "away"
      - RACE: "driver"
      - TOURNAMENT_FIELD: "player"
    ``external_ids`` 是跨源 ID 映射的关键载体，client 解析时尽量填充已知映射
    （例如 SofaScore payload 里给出的 ESPN id），便于 ExternalIdIndex 跨源合并。
    """

    role: str
    name: str
    score: int | None = None
    position: int | None = None
    display_name: str | None = None
    abbreviation: str | None = None
    short_name: str | None = None
    location: str | None = None
    aliases: tuple[str, ...] = ()
    team: str | None = None
    external_ids: Mapping[str, str] = field(default_factory=dict)

    def match_aliases(self) -> tuple[str, ...]:
        """跨源用于 market 文本匹配的别名集合（保持原有拼接规则）。"""

        candidates = [
            self.name,
            self.display_name,
            self.abbreviation,
            self.short_name,
            self.location,
            self.team,
            *self.aliases,
        ]
        if self.location and self.name:
            candidates.append(f"{self.location} {self.name}")
        seen: set[str] = set()
        result: list[str] = []
        for alias in candidates:
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
    """棒球比赛当前局面。

    home_inning_runs / away_inning_runs：各局得分，下标 0 = 第 1 局。值为 None
    表示该局尚未开始或数据缺失。供分局总分（首局、前 5 局 F5 等）盘口判定。
    """

    current_inning: int | None = None
    inning_half: str | None = None
    outs: int | None = None
    offense_team: str | None = None
    defense_team: str | None = None
    occupied_bases: tuple[int, ...] = ()
    home_inning_runs: tuple[int | None, ...] = ()
    away_inning_runs: tuple[int | None, ...] = ()


@dataclass(frozen=True, slots=True)
class BasketballGameState:
    """篮球比赛分节状态。

    current_period：当前节（1-4，5+ 为加时）。
    home_quarter_scores / away_quarter_scores：各节得分，下标 0 = 第 1 节。
    None 表示该节尚未开始或数据缺失。供分场盘口（上半场 1H 等）判定。
    """

    current_period: int | None = None
    home_quarter_scores: tuple[int | None, ...] = ()
    away_quarter_scores: tuple[int | None, ...] = ()


@dataclass(frozen=True, slots=True)
class TennisGameState:
    """网球比赛盘分、局分和即时分状态。"""

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

    def sets_won_for(self, side: str) -> int:
        key = str(side)
        if key == "home":
            return self.home_sets_won
        if key == "away":
            return self.away_sets_won
        return 0

    def current_set_games_for(self, side: str) -> int | None:
        key = str(side)
        if key == "home":
            return self.home_current_set_games
        if key == "away":
            return self.away_current_set_games
        return None


@dataclass(frozen=True, slots=True)
class SoccerGameState:
    """足球比赛节奏状态。

    period 取值：first_half / second_half / extra_time / penalties；
    clock_minutes 是本节内分钟（不含补时）。
    """

    period: str | None = None
    clock_minutes: int | None = None
    added_minutes: int | None = None
    home_red_cards: int = 0
    away_red_cards: int = 0
    home_yellow_cards: int = 0
    away_yellow_cards: int = 0
    last_event_minute: int | None = None
    # 半场比分：仅在半场结束后由数据源给出；两者均非 None 即表示半场已锁定。
    home_halftime_score: int | None = None
    away_halftime_score: int | None = None


@dataclass(frozen=True, slots=True)
class VolleyballGameState:
    """排球比赛盘分状态（best-of-5，每盘 25 分，决胜盘 15 分）。"""

    home_sets_won: int = 0
    away_sets_won: int = 0
    current_set: int | None = None
    home_current_set_points: int | None = None
    away_current_set_points: int | None = None
    set_scores: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class HandballGameState:
    """手球比赛半场状态。"""

    period: str | None = None
    clock_minutes: int | None = None
    home_period1: int | None = None
    away_period1: int | None = None


@dataclass(frozen=True, slots=True)
class RugbyGameState:
    """橄榄球比赛半场状态。"""

    period: str | None = None
    clock_minutes: int | None = None
    home_period1: int | None = None
    away_period1: int | None = None


@dataclass(frozen=True, slots=True)
class MMAFightState:
    """MMA / 拳击格斗当前回合状态。boxing 复用此模型。"""

    current_round: int | None = None
    total_rounds: int | None = None
    time_in_round: str | None = None
    result_method: str | None = None
    winner_side: str | None = None


@dataclass(frozen=True, slots=True)
class EsportsGameState:
    """电子竞技 best-of-N 系列赛状态。"""

    best_of: int | None = None
    current_map_index: int | None = None
    home_maps_won: int = 0
    away_maps_won: int = 0
    home_current_map_score: int | None = None
    away_current_map_score: int | None = None
    map_winners: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CricketGameState:
    """板球比赛局面。整数化 overs/balls 避免 12.3 浮点表示带来的精度问题。"""

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
class RaceState:
    """赛车/竞速类比赛 race-level 状态。

    driver 位次信息在 ``LiveEvent.participants[role="driver"]`` 中按 position 携带，
    这里只补充 race 全场字段（圈数、leader、status_flag 如 GREEN/YELLOW/RED/SC 等）。
    """

    leader_driver: str | None = None
    leader_team: str | None = None
    laps_completed: int | None = None
    total_laps: int | None = None
    status_flag: str | None = None


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """聚合融合时被压制的源记录。

    一等字段，避免下游用 ``source_payload.get("source_conflicts", ())`` 做
    duck-typing fallback。``decided_by`` 标注融合判定依据：majority / trusted_source /
    median / priority_tiebreak / freshness。
    """

    field: str
    winner_source: str
    winner_value: Any
    loser_source: str
    loser_value: Any
    decided_by: str


@dataclass(frozen=True, slots=True)
class LiveEvent:
    """统一的直播事件归一化模型。

    单源 client 输出为单源 LiveEvent；经 ExternalIdIndex + aggregate 融合后得到
    全源融合形态：``source`` 为融合主源，``contributing_sources`` 列所有贡献源，
    ``source_conflicts`` 记录被压制的字段，``external_ids`` 合并所有源的外部 ID。
    """

    source: str
    source_event_id: str
    kind: LiveEventKind
    league: str
    sport: str
    participants: tuple[Participant, ...]
    status: SportsLiveGameStatus
    period: str = ""
    seconds_remaining: int | None = None
    raw_status: str | None = None
    observed_at: datetime | None = None
    event_start_time: datetime | None = None
    event_name: str = ""
    external_ids: Mapping[str, str] = field(default_factory=dict)
    baseball_state: BaseballGameState | None = None
    basketball_state: BasketballGameState | None = None
    tennis_state: TennisGameState | None = None
    soccer_state: SoccerGameState | None = None
    esports_state: EsportsGameState | None = None
    volleyball_state: VolleyballGameState | None = None
    cricket_state: CricketGameState | None = None
    race_state: RaceState | None = None
    handball_state: HandballGameState | None = None
    rugby_state: RugbyGameState | None = None
    mma_state: MMAFightState | None = None
    source_conflicts: tuple[ConflictRecord, ...] = ()
    contributing_sources: tuple[str, ...] = ()
    source_payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def home(self) -> Participant | None:
        """team_match 主队便捷访问器；其他 kind 返回 None。"""

        if self.kind != LiveEventKind.TEAM_MATCH:
            return None
        for participant in self.participants:
            if participant.role == "home":
                return participant
        return None

    @property
    def away(self) -> Participant | None:
        if self.kind != LiveEventKind.TEAM_MATCH:
            return None
        for participant in self.participants:
            if participant.role == "away":
                return participant
        return None

    @property
    def total_score(self) -> int:
        return sum(participant.score or 0 for participant in self.participants)

    @property
    def drivers(self) -> tuple[Participant, ...]:
        """race / tournament_field 的参与者按 position 升序。"""

        if self.kind == LiveEventKind.TEAM_MATCH:
            return ()
        ordered = sorted(
            self.participants,
            key=lambda p: (p.position is None, p.position or 0),
        )
        return tuple(ordered)

    def as_payload(self) -> dict[str, Any]:
        """返回不含外部原始 payload 的可审计内部快照。"""

        return {
            "source": self.source,
            "source_event_id": self.source_event_id,
            "kind": self.kind.value,
            "league": self.league,
            "sport": self.sport,
            "participants": [jsonable(p) for p in self.participants],
            "status": self.status.value,
            "period": self.period,
            "seconds_remaining": self.seconds_remaining,
            "raw_status": self.raw_status,
            "observed_at": None if self.observed_at is None else self.observed_at.isoformat(),
            "event_start_time": (
                None if self.event_start_time is None else self.event_start_time.isoformat()
            ),
            "event_name": self.event_name,
            "external_ids": dict(self.external_ids),
            "baseball_state": None if self.baseball_state is None else jsonable(self.baseball_state),
            "tennis_state": None if self.tennis_state is None else jsonable(self.tennis_state),
            "soccer_state": None if self.soccer_state is None else jsonable(self.soccer_state),
            "esports_state": None if self.esports_state is None else jsonable(self.esports_state),
            "volleyball_state": None if self.volleyball_state is None else jsonable(self.volleyball_state),
            "cricket_state": None if self.cricket_state is None else jsonable(self.cricket_state),
            "race_state": None if self.race_state is None else jsonable(self.race_state),
            "handball_state": None if self.handball_state is None else jsonable(self.handball_state),
            "rugby_state": None if self.rugby_state is None else jsonable(self.rugby_state),
            "mma_state": None if self.mma_state is None else jsonable(self.mma_state),
            "source_conflicts": [jsonable(c) for c in self.source_conflicts],
            "contributing_sources": list(self.contributing_sources),
        }


@dataclass(frozen=True, slots=True)
class SportsLiveSourceStatus:
    """一次聚合同步中单个外部源的状态摘要。"""

    source: str
    success: bool
    health: SportsLiveSourceHealth = SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
    events_seen: int = 0
    observed_at: datetime | None = None
    last_error: str | None = None
    cooldown_until: datetime | None = None
    consecutive_failures: int = 0

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True, slots=True)
class SportsLiveSnapshot:
    """一次外部比分源拉取后的归一化事件集合。"""

    source: str
    observed_at: datetime
    events: tuple[LiveEvent, ...]
    source_statuses: tuple[SportsLiveSourceStatus, ...] = ()


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
    last_events_seen: int = 0
    last_markets_seen: int = 0
    last_matches: int = 0
    last_records_written: int = 0
    last_unmatched_markets: int = 0
    last_entry_signals_published: int = 0
    leagues: tuple[str, ...] = ()
    source_statuses: tuple[SportsLiveSourceStatus, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)
