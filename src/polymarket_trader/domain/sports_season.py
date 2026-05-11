"""赛季级体育状态域类型。

与 ``sports_live`` 兄弟分层：``sports_live`` 服务于秒级实盘单场状态，
``sports_season`` 服务于小时/天级赛季积分、系列赛分、隐含概率等长周期信号，
是 outright 反向定价的数据基座。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.serialization import jsonable


@dataclass(frozen=True, slots=True)
class SeasonStandingRow:
    """赛季积分榜单行。

    ``games_back`` 仅在分区/分组榜适用；``conference`` / ``division`` 是源附加分组。
    """

    team: str
    wins: int = 0
    losses: int = 0
    ties: int = 0
    win_pct: Decimal | None = None
    games_back: Decimal | None = None
    conference: str | None = None
    division: str | None = None
    seed: int | None = None
    clinched: str | None = None


@dataclass(frozen=True, slots=True)
class SeasonStandings:
    """单个联赛单赛季的积分榜快照。"""

    league: str
    season_id: str
    observed_at: datetime
    rows: tuple[SeasonStandingRow, ...] = ()
    source: str = ""


@dataclass(frozen=True, slots=True)
class SeriesScore:
    """系列赛（NBA/NHL 季后赛、MLB 世界大赛等 best-of-N）当前比分。"""

    league: str
    series_id: str
    home_team: str
    away_team: str
    home_wins: int = 0
    away_wins: int = 0
    best_of: int | None = None
    status: str | None = None
    observed_at: datetime | None = None
    source: str = ""


@dataclass(frozen=True, slots=True)
class SeasonSnapshot:
    """一次赛季状态拉取的归一化结果。"""

    observed_at: datetime
    standings: tuple[SeasonStandings, ...] = ()
    series: tuple[SeriesScore, ...] = ()
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True, slots=True)
class SeasonOddsSnapshot:
    """单个 outright market 的赛季隐含概率快照。

    ``fair_probabilities`` 是 outcome 名 → de-vig 后概率的映射（应该求和到 1.0，
    但不强制：偏差由 ``source_conflicts`` 标注）；``market_key`` 是策略侧能
    用来匹配 Polymarket market 的稳定 key（slug / event_slug）。
    """

    market_key: str
    fair_probabilities: Mapping[str, Decimal]
    observed_at: datetime
    source: str
    source_event_id: str | None = None
    source_conflicts: bool = False
    raw_payload: Mapping[str, Any] = field(default_factory=dict)

    def probability_for(self, outcome: str) -> Decimal | None:
        if not outcome:
            return None
        return self.fair_probabilities.get(outcome) or self.fair_probabilities.get(outcome.lower())


@dataclass(frozen=True, slots=True)
class SeasonStateSyncStatus:
    """季节状态同步器的可观测快照。"""

    enabled: bool
    source: str
    running: bool = False
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_standings_count: int = 0
    last_series_count: int = 0
    leagues: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)
