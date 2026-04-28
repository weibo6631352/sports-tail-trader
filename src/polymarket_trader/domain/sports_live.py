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
