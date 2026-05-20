"""体育直播状态聚合器（健康检查 + 超时管理 + 指数退避冷却）。

职责：
- 并行拉取所有注册 provider（独立超时 + 指数退避冷却 + 冷却期缓存复用）；
- 单源时直接 pass-through，无需跨源融合；
- per-league 源亲和：``league_source_priority`` 注入，league-aware 加权（多源场景预留）；
- 融合证据用 ``LiveEvent.source_conflicts`` 一等字段记录，``contributing_sources`` 暴露所有源。
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from statistics import median_low
from typing import Any

from polymarket_trader.domain.sports_live import (
    ConflictRecord,
    LiveEvent,
    LiveEventKind,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
    SportsLiveSourceStatus,
)
from polymarket_trader.infra.sports.common import utc_now

SportsLiveSnapshotProvider = Callable[[], Awaitable[SportsLiveSnapshot]]
SportsLiveCloser = Callable[[], Awaitable[None]]

_STATUS_PRIORITY = {
    SportsLiveGameStatus.LIVE: 50,
    SportsLiveGameStatus.PAUSED: 40,
    SportsLiveGameStatus.SCHEDULED: 30,
    SportsLiveGameStatus.UNKNOWN: 20,
    SportsLiveGameStatus.POSTPONED: 10,
    SportsLiveGameStatus.DISPUTED: 10,
    SportsLiveGameStatus.RETIRED: 5,
    SportsLiveGameStatus.CANCELLED: 5,
    SportsLiveGameStatus.ENDED: 0,
}

# 全局源优先级表（fallback）。league_source_priority 注入时按 league 覆盖。
_DEFAULT_SOURCE_PRIORITY: Mapping[str, int] = {
    "goalserve": 80,
}

# 一律视为官方（高可信）的源；冲突时同等 priority 下仍偏向 official。
_DEFAULT_OFFICIAL_SOURCES: frozenset[str] = frozenset({"goalserve"})

# 加权融合中的时间衰减：observed_at 越久权重越低（半衰期 5 分钟）。
_FRESHNESS_HALF_LIFE_S = 300.0


@dataclass
class _ProviderCooldown:
    """单个 provider 的连续失败累计与冷却时间窗。

    指数退避：第 N 次连续失败后冷却 ``base * 2^(N-1)``，上限 ``cap``；
    冷却累计超过 eviction_s 进入 EVICTED 状态，由 worker 上报生命周期。
    """

    consecutive_failures: int = 0
    cooldown_until: datetime | None = None
    first_failure_at: datetime | None = None
    last_error: str | None = None
    last_observed_at: datetime | None = None
    evicted: bool = False


class SportsLiveAggregateClient:
    """并行读取多个体育直播源，输出单个归一化快照。"""

    def __init__(
        self,
        *,
        providers: Sequence[tuple[str, SportsLiveSnapshotProvider]],
        closers: Sequence[SportsLiveCloser] = (),
        now_provider: Callable[[], datetime] | None = None,
        provider_timeout_s: float = 8.0,
        cooldown_base_s: float = 60.0,
        cooldown_cap_s: float = 600.0,
        eviction_s: float = 1800.0,
        cooldown_failure_threshold: int = 3,
        league_source_priority: Mapping[str, Sequence[str]] | None = None,
        trusted_sources: Sequence[str] | None = None,
    ) -> None:
        self._providers = tuple((str(source).strip().lower(), provider) for source, provider in providers)
        self._closers = tuple(closers)
        self._now_provider = now_provider
        self._provider_timeout_s = max(0.1, float(provider_timeout_s))
        self._cooldown_base_s = max(1.0, float(cooldown_base_s))
        self._cooldown_cap_s = max(self._cooldown_base_s, float(cooldown_cap_s))
        self._eviction_s = max(self._cooldown_cap_s, float(eviction_s))
        self._cooldown_failure_threshold = max(1, int(cooldown_failure_threshold))
        self._provider_cache: dict[str, SportsLiveSnapshot] = {}
        self._cooldowns: dict[str, _ProviderCooldown] = {}
        self._league_priority: dict[str, tuple[str, ...]] = {
            self._league_key(league): tuple(str(s).strip().lower() for s in sources)
            for league, sources in (league_source_priority or {}).items()
        }
        self._trusted_sources: frozenset[str] = (
            frozenset(str(s).strip().lower() for s in trusted_sources)
            if trusted_sources
            else _DEFAULT_OFFICIAL_SOURCES
        )

    async def aclose(self) -> None:
        if not self._closers:
            return
        await asyncio.gather(*(closer() for closer in self._closers), return_exceptions=True)

    async def list_events(self) -> SportsLiveSnapshot:
        """拉取所有来源，失败来源进入 source_statuses 但不阻断健康来源。

        失败累计超过阈值进入冷却（指数退避，最长 ``cooldown_cap_s``）；冷却期内
        被跳过但缓存数据仍被复用，避免突然失去覆盖。冷却累计超过 ``eviction_s``
        标记 EVICTED，由 worker 发出生命周期事件。
        """

        observed_at = utc_now(self._now_provider)
        active: list[tuple[str, SportsLiveSnapshotProvider]] = []
        cooldown_statuses: list[SportsLiveSourceStatus] = []
        for source, provider in self._providers:
            cooldown = self._cooldowns.get(source)
            if cooldown and cooldown.cooldown_until is not None and cooldown.cooldown_until > observed_at:
                cooldown_statuses.append(self._cooldown_status(source, cooldown, observed_at))
                continue
            active.append((source, provider))

        tasks = [
            asyncio.create_task(
                _provider_snapshot_with_timeout(provider, timeout_s=self._provider_timeout_s),
                name=f"sports-live-source:{source}",
            )
            for source, provider in active
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else ()

        source_statuses: list[SportsLiveSourceStatus] = list(cooldown_statuses)
        all_events: list[LiveEvent] = []

        for (source, _provider), result in zip(active, results, strict=True):
            if isinstance(result, Exception):
                self._record_failure(source, error=str(result), observed_at=observed_at)
                cooldown = self._cooldowns.get(source)
                cached = self._provider_cache.get(source)
                if cached is not None:
                    source_statuses.append(_cached_source_status(source, cached, last_error=str(result)))
                    all_events.extend(cached.events)
                else:
                    source_statuses.append(
                        SportsLiveSourceStatus(
                            source=source,
                            success=False,
                            health=SportsLiveSourceHealth.FAILED,
                            events_seen=0,
                            observed_at=observed_at,
                            last_error=str(result),
                            cooldown_until=cooldown.cooldown_until if cooldown else None,
                            consecutive_failures=cooldown.consecutive_failures if cooldown else 1,
                        )
                    )
                continue
            self._record_success(source)
            status = _source_status_from_snapshot(source, result)
            source_statuses.append(status)
            if status.success:
                self._provider_cache[source] = result
            all_events.extend(result.events)

        # 冷却期内复用 cached：让上游 worker 仍能感知历史比赛，不发起网络请求。
        for status in cooldown_statuses:
            cached = self._provider_cache.get(status.source)
            if cached is not None and status.health == SportsLiveSourceHealth.COOLDOWN:
                all_events.extend(cached.events)

        fused = self._fuse(tuple(all_events))
        return SportsLiveSnapshot(
            source="sports_live_aggregate",
            observed_at=observed_at,
            events=fused,
            source_statuses=tuple(source_statuses),
        )

    def _fuse(self, events: tuple[LiveEvent, ...]) -> tuple[LiveEvent, ...]:
        """单源时 pass-through（标记 contributing_sources）；多源时按权重融合。"""

        if not events:
            return ()

        # 按 (kind, league, sport, team-pair/event-name, start-bucket) 分组
        text_keys: dict[tuple[str, str, str, str, str | None], int] = {}
        groups: list[list[int]] = []
        for idx, event in enumerate(events):
            key = _text_key(event)
            existing = text_keys.get(key)
            if existing is None:
                text_keys[key] = len(groups)
                groups.append([idx])
            else:
                groups[existing].append(idx)

        fused_events: list[LiveEvent] = []
        for indices in groups:
            if not indices:
                continue
            members = tuple(events[i] for i in indices)
            fused_events.append(self._fuse_group(members))
        return tuple(fused_events)

    def _fuse_group(self, members: tuple[LiveEvent, ...]) -> LiveEvent:
        """同一逻辑场的多源融合。

        - 单成员：直接返回，contributing_sources=[source]。
        - 多成员：按 league-aware priority + freshness 加权；
          - status: weighted majority vote；
          - 比分（team_match）：trusted source 优先，否则取所有源 per-side 中位数；
          - leader_driver（race）：trusted source 优先，否则 priority 高者；
          - 其他字段：取主源（priority+freshness 最高者）的值；
          - external_ids / contributing_sources / source_conflicts 全并入主源。
        """

        if len(members) == 1:
            sole = members[0]
            return replace(
                sole,
                contributing_sources=(sole.source,),
                source_conflicts=sole.source_conflicts,
            )

        league = members[0].league
        primary = max(members, key=lambda e: self._weight(e, league))

        # status: weighted majority vote
        status_value, status_conflicts = self._fuse_status(members, primary, league)

        # score / leader fields
        score_conflicts: list[ConflictRecord] = []
        participants = primary.participants
        race_state = primary.race_state
        if primary.kind == LiveEventKind.TEAM_MATCH:
            participants, score_conflicts = self._fuse_team_scores(members, primary, league)
        elif primary.kind == LiveEventKind.RACE:
            race_state, race_conflicts = self._fuse_race(members, primary, league)
            score_conflicts = race_conflicts

        # 合并 external_ids
        merged_ids: dict[str, str] = dict(primary.external_ids)
        for member in members:
            for scheme, value in member.external_ids.items():
                merged_ids.setdefault(scheme, value)

        # 主源缺少 baseball_state 时，从权重最高的有该字段的成员补充。
        # 官方 MLB API 源有详细局面数据，但 ESPN 优先级可能偶尔抢主源；
        # 融合时不应丢弃可用的结构化局面。
        baseball_state = primary.baseball_state
        if baseball_state is None and primary.kind == LiveEventKind.TEAM_MATCH:
            for member in sorted(members, key=lambda e: self._weight(e, league), reverse=True):
                if member.baseball_state is not None:
                    baseball_state = member.baseball_state
                    break

        contributing = tuple(sorted({m.source for m in members}))
        all_conflicts = (*primary.source_conflicts, *status_conflicts, *score_conflicts)

        return replace(
            primary,
            status=status_value,
            participants=participants,
            race_state=race_state,
            baseball_state=baseball_state,
            external_ids=merged_ids,
            contributing_sources=contributing,
            source_conflicts=all_conflicts,
        )

    def _fuse_status(
        self,
        members: tuple[LiveEvent, ...],
        primary: LiveEvent,
        league: str,
    ) -> tuple[SportsLiveGameStatus, tuple[ConflictRecord, ...]]:
        """加权 majority vote 决 status；trusted 源单独说 ENDED 仍取 ENDED（避免假活跃）。"""

        # trusted 源若主张 ENDED 且其他源仍 LIVE，trusted 优先（防漏抓返场风险已由 audit 留痕）
        trusted_ended = [
            m for m in members if m.source in self._trusted_sources and m.status == SportsLiveGameStatus.ENDED
        ]
        if trusted_ended:
            best_trusted = max(trusted_ended, key=lambda e: self._weight(e, league))
            conflicts: list[ConflictRecord] = []
            for member in members:
                if member.source == best_trusted.source:
                    continue
                if member.status == SportsLiveGameStatus.ENDED:
                    continue
                conflicts.append(
                    ConflictRecord(
                        field="status",
                        winner_source=best_trusted.source,
                        winner_value=best_trusted.status.value,
                        loser_source=member.source,
                        loser_value=member.status.value,
                        decided_by="trusted_source",
                    )
                )
            return best_trusted.status, tuple(conflicts)

        # 一般情形：weighted majority vote
        tallies: Counter[SportsLiveGameStatus] = Counter()
        weight_by_status: dict[SportsLiveGameStatus, float] = {}
        sources_by_status: dict[SportsLiveGameStatus, list[LiveEvent]] = {}
        for member in members:
            w = self._weight(member, league)
            tallies[member.status] += 1
            weight_by_status[member.status] = weight_by_status.get(member.status, 0.0) + w
            sources_by_status.setdefault(member.status, []).append(member)
        # 按权重排序，权重并列再用 STATUS_PRIORITY 兜底
        winner_status = max(
            weight_by_status.keys(),
            key=lambda s: (weight_by_status[s], _STATUS_PRIORITY.get(s, 0)),
        )
        decided_by = "majority" if tallies[winner_status] > 1 else "priority_tiebreak"
        conflicts = []
        winner_examples = sources_by_status[winner_status]
        winner_example = max(winner_examples, key=lambda e: self._weight(e, league))
        for member in members:
            if member.status == winner_status:
                continue
            conflicts.append(
                ConflictRecord(
                    field="status",
                    winner_source=winner_example.source,
                    winner_value=winner_status.value,
                    loser_source=member.source,
                    loser_value=member.status.value,
                    decided_by=decided_by,
                )
            )
        return winner_status, tuple(conflicts)

    def _fuse_team_scores(
        self,
        members: tuple[LiveEvent, ...],
        primary: LiveEvent,
        league: str,
    ) -> tuple[tuple[Any, ...], list[ConflictRecord]]:
        """team_match 比分融合：trusted source 优先；否则取 per-side 中位数。"""

        if primary.home is None or primary.away is None:
            return primary.participants, []

        home_scores: list[tuple[str, int, float]] = []
        away_scores: list[tuple[str, int, float]] = []
        for member in members:
            if member.kind != LiveEventKind.TEAM_MATCH:
                continue
            if member.home is not None and member.home.score is not None:
                home_scores.append((member.source, member.home.score, self._weight(member, league)))
            if member.away is not None and member.away.score is not None:
                away_scores.append((member.source, member.away.score, self._weight(member, league)))

        home_score, home_decided = self._pick_score(home_scores)
        away_score, away_decided = self._pick_score(away_scores)

        new_participants = []
        for participant in primary.participants:
            if participant.role == "home" and home_score is not None:
                new_participants.append(replace(participant, score=home_score))
            elif participant.role == "away" and away_score is not None:
                new_participants.append(replace(participant, score=away_score))
            else:
                new_participants.append(participant)

        conflicts: list[ConflictRecord] = []
        winner_home = next((s for s, v, _w in home_scores if v == home_score), primary.source)
        winner_away = next((s for s, v, _w in away_scores if v == away_score), primary.source)
        for source, value, _w in home_scores:
            if value != home_score:
                conflicts.append(
                    ConflictRecord(
                        field="home_score",
                        winner_source=winner_home,
                        winner_value=home_score,
                        loser_source=source,
                        loser_value=value,
                        decided_by=home_decided,
                    )
                )
        for source, value, _w in away_scores:
            if value != away_score:
                conflicts.append(
                    ConflictRecord(
                        field="away_score",
                        winner_source=winner_away,
                        winner_value=away_score,
                        loser_source=source,
                        loser_value=value,
                        decided_by=away_decided,
                    )
                )
        return tuple(new_participants), conflicts

    def _pick_score(self, scores: list[tuple[str, int, float]]) -> tuple[int | None, str]:
        """trusted 优先；否则中位数（中位数防单源跳号比 mean 稳）。"""

        if not scores:
            return None, "none"
        trusted_only = [(s, v) for s, v, _w in scores if s in self._trusted_sources]
        if trusted_only:
            # 取 trusted 中权重最高那条 score
            best = max(scores, key=lambda x: (x[0] in self._trusted_sources, x[2]))
            return best[1], "trusted_source"
        return median_low([v for _s, v, _w in scores]), "median"

    def _fuse_race(
        self,
        members: tuple[LiveEvent, ...],
        primary: LiveEvent,
        league: str,
    ) -> tuple[Any, list[ConflictRecord]]:
        """race 状态融合：leader 取 trusted 源；否则取主源。"""

        race_members = [m for m in members if m.race_state is not None]
        if not race_members:
            return primary.race_state, []
        trusted = [m for m in race_members if m.source in self._trusted_sources]
        chosen = (
            max(trusted, key=lambda e: self._weight(e, league))
            if trusted
            else max(race_members, key=lambda e: self._weight(e, league))
        )
        decided_by = "trusted_source" if trusted else "priority_tiebreak"
        conflicts: list[ConflictRecord] = []
        for member in race_members:
            if member.source == chosen.source:
                continue
            if member.race_state is None:
                continue
            if (member.race_state.leader_driver or "") != (chosen.race_state.leader_driver or ""):
                conflicts.append(
                    ConflictRecord(
                        field="leader_driver",
                        winner_source=chosen.source,
                        winner_value=chosen.race_state.leader_driver,
                        loser_source=member.source,
                        loser_value=member.race_state.leader_driver,
                        decided_by=decided_by,
                    )
                )
        return chosen.race_state, conflicts

    def _weight(self, event: LiveEvent, league: str) -> float:
        """加权融合用：源优先级 × 时间衰减（半衰期 ``_FRESHNESS_HALF_LIFE_S`` 秒）。"""

        priority = float(self._source_priority(event.source, league))
        observed = event.observed_at
        if observed is None:
            return priority
        now = utc_now(self._now_provider)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age_s = max(0.0, (now - observed).total_seconds())
        decay = 0.5 ** (age_s / _FRESHNESS_HALF_LIFE_S)
        return priority * decay

    def _source_priority(self, source: str, league: str) -> int:
        """league-aware 源优先级：先查 league 表，缺失则回退全局表。

        league 表里的位置（前面更高）映射成 priority：第 1 位 100，第 2 位 90，...
        """

        normalized = str(source or "").split(":", maxsplit=1)[0].strip().lower()
        league_key = self._league_key(league)
        league_order = self._league_priority.get(league_key)
        if league_order is not None:
            try:
                rank = league_order.index(normalized)
                return max(10, 100 - rank * 10)
            except ValueError:
                pass
        return _DEFAULT_SOURCE_PRIORITY.get(normalized, 0)

    @staticmethod
    def _league_key(league: str) -> str:
        return str(league or "").strip().upper()

    def _record_failure(self, source: str, *, error: str, observed_at: datetime) -> None:
        cooldown = self._cooldowns.setdefault(source, _ProviderCooldown())
        cooldown.consecutive_failures += 1
        cooldown.last_error = error
        cooldown.last_observed_at = observed_at
        if cooldown.first_failure_at is None:
            cooldown.first_failure_at = observed_at
        if cooldown.consecutive_failures < self._cooldown_failure_threshold:
            cooldown.cooldown_until = None
            return
        backoff_index = cooldown.consecutive_failures - self._cooldown_failure_threshold
        delay = min(self._cooldown_base_s * (2 ** backoff_index), self._cooldown_cap_s)
        cooldown.cooldown_until = observed_at + timedelta(seconds=delay)
        elapsed = (observed_at - (cooldown.first_failure_at or observed_at)).total_seconds()
        if elapsed >= self._eviction_s:
            cooldown.evicted = True

    def _record_success(self, source: str) -> None:
        cooldown = self._cooldowns.get(source)
        if cooldown is None:
            return
        cooldown.consecutive_failures = 0
        cooldown.cooldown_until = None
        cooldown.first_failure_at = None
        cooldown.last_error = None
        cooldown.evicted = False

    def _cooldown_status(
        self,
        source: str,
        cooldown: _ProviderCooldown,
        observed_at: datetime,
    ) -> SportsLiveSourceStatus:
        health = SportsLiveSourceHealth.EVICTED if cooldown.evicted else SportsLiveSourceHealth.COOLDOWN
        return SportsLiveSourceStatus(
            source=source,
            success=False,
            health=health,
            events_seen=0,
            observed_at=observed_at,
            last_error=cooldown.last_error,
            cooldown_until=cooldown.cooldown_until,
            consecutive_failures=cooldown.consecutive_failures,
        )


async def _provider_snapshot_with_timeout(
    provider: SportsLiveSnapshotProvider,
    *,
    timeout_s: float,
) -> SportsLiveSnapshot:
    """单个数据源超时后降级，避免阻塞整轮实盘状态同步。"""

    try:
        return await asyncio.wait_for(provider(), timeout=timeout_s)
    except TimeoutError as exc:
        raise TimeoutError(f"provider_timeout after {timeout_s:.3f}s") from exc


def _cached_source_status(
    source: str,
    snapshot: SportsLiveSnapshot,
    *,
    last_error: str,
) -> SportsLiveSourceStatus:
    return SportsLiveSourceStatus(
        source=source,
        success=True,
        health=SportsLiveSourceHealth.CACHED,
        events_seen=len(snapshot.events),
        observed_at=snapshot.observed_at,
        last_error=last_error,
    )


def _source_status_from_snapshot(source: str, snapshot: SportsLiveSnapshot) -> SportsLiveSourceStatus:
    for status in snapshot.source_statuses:
        if status.source == source:
            return replace(
                status,
                events_seen=len(snapshot.events),
                observed_at=status.observed_at or snapshot.observed_at,
            )
    health = (
        SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
        if snapshot.events
        else SportsLiveSourceHealth.SUCCESS_EMPTY
    )
    return SportsLiveSourceStatus(
        source=source,
        success=True,
        health=health,
        events_seen=len(snapshot.events),
        observed_at=snapshot.observed_at,
    )


def _text_key(event: LiveEvent) -> tuple[str, str, str, str, str | None]:
    """单成员组的 text-merge 兜底键：(kind, league, sport, identity, start_bucket)。

    team_match：identity = 排序的 home/away 队名集合；
    race / tournament_field：identity = 规范化 event_name；
    start_bucket：UTC 小时级（None 表示未知，None 不强匹配）。
    """

    kind = event.kind.value
    league = (event.league or "").strip().upper()
    sport = (event.sport or "").strip().lower()
    if event.kind == LiveEventKind.TEAM_MATCH and event.home and event.away:
        identity = "|".join(sorted([_team_key(event.home), _team_key(event.away)]))
    else:
        identity = _normalize(event.event_name)
    bucket = _start_bucket(event)
    return (kind, league, sport, identity, bucket)


def _team_key(participant: Any) -> str:
    """生成一个用于 text-fallback dedup 的稳定 team-key。

    优先取 ``short_name``——多源约定 short_name 是无地理前缀的"绰号"形式
    （Utah Mammoth → Mammoth；Vegas Golden Knights → Golden Knights），跨源
    最稳定。回退到 abbreviation / display_name / name。所有候选都先 normalize。
    """

    candidates = [
        participant.short_name,
        participant.abbreviation,
        participant.display_name,
        participant.name,
    ]
    for candidate in candidates:
        normalized = _normalize(candidate)
        if normalized and normalized != "unknown":
            return normalized
    return "unknown"


def _normalize(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    return text or "unknown"


def _start_bucket(event: LiveEvent) -> str | None:
    if event.event_start_time is not None:
        ts = event.event_start_time
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
    return None
