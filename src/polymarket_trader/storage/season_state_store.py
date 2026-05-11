"""赛季状态内存 store，键 ``(league, season_id)``。

与 ``EntryMetadataStore`` 解耦：season state cadence 与决策热路径完全不同，
混入 EntryMetadataStore 会让秒级读和小时级写互相干扰。本 store 只面向赛季
worker 写、outright 评估读。
"""

from __future__ import annotations

from threading import RLock
from typing import Iterator, Mapping

from polymarket_trader.domain.sports_season import (
    SeasonStandings,
    SeriesScore,
)


class SeasonStateStore:
    """``(league, season_id)`` → 最近一次的 SeasonStandings / SeriesScore。

    内部用 RLock 而不是 asyncio.Lock：写入只由 P2 worker 单线程进入，读由策略
    评估同步取（不 await），用 RLock 让两端互不阻塞、也不会反向阻塞主链路。
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._standings: dict[tuple[str, str], SeasonStandings] = {}
        self._series: dict[tuple[str, str], SeriesScore] = {}

    def upsert_standings(self, standings: SeasonStandings) -> None:
        key = (_normalize(standings.league), _normalize(standings.season_id))
        with self._lock:
            existing = self._standings.get(key)
            if existing is not None and existing.observed_at >= standings.observed_at:
                # 后到的旧数据不能覆盖新数据；P2 worker 偶尔会出现乱序。
                return
            self._standings[key] = standings

    def upsert_series(self, series: SeriesScore) -> None:
        key = (_normalize(series.league), _normalize(series.series_id))
        with self._lock:
            existing = self._series.get(key)
            if (
                existing is not None
                and existing.observed_at is not None
                and series.observed_at is not None
                and existing.observed_at >= series.observed_at
            ):
                return
            self._series[key] = series

    def get_standings(self, league: str, season_id: str) -> SeasonStandings | None:
        with self._lock:
            return self._standings.get((_normalize(league), _normalize(season_id)))

    def get_series(self, league: str, series_id: str) -> SeriesScore | None:
        with self._lock:
            return self._series.get((_normalize(league), _normalize(series_id)))

    def all_standings(self) -> tuple[SeasonStandings, ...]:
        with self._lock:
            return tuple(self._standings.values())

    def all_series(self) -> tuple[SeriesScore, ...]:
        with self._lock:
            return tuple(self._series.values())

    def standings_by_league(self) -> Mapping[str, SeasonStandings]:
        """league → 最新该联赛 standings（任意 season_id 的最新一份）。"""

        with self._lock:
            grouped: dict[str, SeasonStandings] = {}
            for (league, _season_id), standings in self._standings.items():
                existing = grouped.get(league)
                if existing is None or standings.observed_at > existing.observed_at:
                    grouped[league] = standings
            return grouped

    def __iter__(self) -> Iterator[SeasonStandings]:
        return iter(self.all_standings())


def _normalize(value: str) -> str:
    return str(value or "").strip().lower()
