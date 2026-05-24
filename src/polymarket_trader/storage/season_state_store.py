"""赛季状态内存 store，键 ``(league, season_id)``。

与 ``EntryMetadataStore`` 解耦：season state cadence 与决策热路径完全不同，
混入 EntryMetadataStore 会让秒级读和小时级写互相干扰。本 store 只面向赛季
worker 写、outright 评估读。
"""

from __future__ import annotations

from collections import OrderedDict
from threading import RLock
from typing import TYPE_CHECKING, Iterator, Mapping

if TYPE_CHECKING:
    from datetime import datetime

from polymarket_trader.domain.sports_season import (
    SeasonStandings,
    SeriesScore,
)

# 上限:30 league × 10 season ≈ 300,5000 留足余量;每 entry ~1KB → ~5MB 硬上限.
# season/series 跨多年累积不会自然清理(season 结束 store 不知道),用 LRU evict 兜底.
_DEFAULT_MAX_ENTRIES = 5_000


class SeasonStateStore:
    """``(league, season_id)`` → 最近一次的 SeasonStandings / SeriesScore。

    内部用 RLock 而不是 asyncio.Lock：写入只由 P2 worker 单线程进入，读由策略
    评估同步取（不 await），用 RLock 让两端互不阻塞、也不会反向阻塞主链路。

    使用 OrderedDict + LRU cap:跨多年 season 累积时自动淘汰最久未更新的条目,
    防止 dict 无限增长(season 结束 store 不知道,只能靠 LRU 兜底).
    """

    def __init__(self, *, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._lock = RLock()
        self._standings: OrderedDict[tuple[str, str], SeasonStandings] = OrderedDict()
        self._series: OrderedDict[tuple[str, str], SeriesScore] = OrderedDict()
        self._max_entries = max_entries

    def upsert_standings(self, standings: SeasonStandings) -> None:
        key = (_normalize(standings.league), _normalize(standings.season_id))
        with self._lock:
            existing = self._standings.get(key)
            if existing is not None and existing.observed_at >= standings.observed_at:
                # 后到的旧数据不能覆盖新数据；P2 worker 偶尔会出现乱序。
                return
            self._standings[key] = standings
            self._standings.move_to_end(key)
            while len(self._standings) > self._max_entries:
                self._standings.popitem(last=False)

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
            self._series.move_to_end(key)
            while len(self._series) > self._max_entries:
                self._series.popitem(last=False)

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

    def prune_stale(
        self,
        *,
        max_age_seconds: float,
        now: "datetime | None" = None,
    ) -> tuple[int, int]:
        """TTL prune:超过 max_age_seconds 没更新的 entry 删除.

        替代精确的 market-lifecycle 绑定(Market 没有 league/season 字段):
        - season 结束后 worker 不再 fetch 该 (league, season) → observed_at 不再更新
        - 超过 max_age_seconds 没更新 → 视为 dead,删除
        - 误删风险:source 偶尔遗漏一次 fetch 也会重新写入,无业务影响
        ``now`` 注入用于测试(默认 datetime.now()).返回 (standings_pruned, series_pruned).
        """
        from datetime import datetime, timezone
        ref_now = now or datetime.now(timezone.utc)
        cutoff = ref_now.timestamp() - max_age_seconds
        pruned_s = pruned_r = 0
        with self._lock:
            stale = [k for k, v in self._standings.items() if v.observed_at.timestamp() < cutoff]
            for k in stale:
                self._standings.pop(k, None)
                pruned_s += 1
            stale = [
                k for k, v in self._series.items()
                if v.observed_at is not None and v.observed_at.timestamp() < cutoff
            ]
            for k in stale:
                self._series.pop(k, None)
                pruned_r += 1
        return pruned_s, pruned_r

    def prune_except(
        self,
        *,
        keep_standings_keys: set[tuple[str, str]] | None = None,
        keep_series_keys: set[tuple[str, str]] | None = None,
    ) -> tuple[int, int]:
        """精确生命周期 prune:清掉不在 keep 集合的 entry.

        worker 每次 sync 后调用,传入"当前活跃 market 所需的 (league, season/series)"
        集合,store 自动 prune 不再需要的历史 entry.
        cap LRU 是兜底,本 API 是精确生命周期管理.返回 (standings_pruned, series_pruned).
        """
        pruned_s = pruned_r = 0
        with self._lock:
            if keep_standings_keys is not None:
                stale = [k for k in self._standings if k not in keep_standings_keys]
                for k in stale:
                    self._standings.pop(k, None)
                    pruned_s += 1
            if keep_series_keys is not None:
                stale = [k for k in self._series if k not in keep_series_keys]
                for k in stale:
                    self._series.pop(k, None)
                    pruned_r += 1
        return pruned_s, pruned_r

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
