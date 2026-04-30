"""多源体育直播状态聚合器。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
import re
from typing import Any

from polymarket_trader.domain.sports_live import (
    SportsLiveGame,
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

_SOURCE_PRIORITY = {
    "nba": 50,
    "nhl": 50,
    "mlb": 50,
    "espn": 40,
    "sofascore": 30,
    "thesportsdb": 20,
}

_OFFICIAL_SOURCES = frozenset({"nba", "nhl", "mlb", "espn"})


class SportsLiveAggregateClient:
    """并行读取多个体育直播源，输出单个归一化快照。"""

    def __init__(
        self,
        *,
        providers: Sequence[tuple[str, SportsLiveSnapshotProvider]],
        closers: Sequence[SportsLiveCloser] = (),
        now_provider: Callable[[], datetime] | None = None,
        provider_timeout_s: float = 8.0,
    ) -> None:
        self._providers = tuple((str(source).strip().lower(), provider) for source, provider in providers)
        self._closers = tuple(closers)
        self._now_provider = now_provider
        self._provider_timeout_s = max(0.1, float(provider_timeout_s))
        self._provider_cache: dict[str, SportsLiveSnapshot] = {}

    async def aclose(self) -> None:
        """关闭聚合器持有的所有底层 client。"""

        if not self._closers:
            return
        await asyncio.gather(*(closer() for closer in self._closers), return_exceptions=True)

    async def list_games(self) -> SportsLiveSnapshot:
        """拉取所有来源，失败来源只进入 source_statuses，不阻断健康来源。"""

        observed_at = utc_now(self._now_provider)
        tasks = [
            asyncio.create_task(
                _provider_snapshot_with_timeout(provider, timeout_s=self._provider_timeout_s),
                name=f"sports-live-source:{source}",
            )
            for source, provider in self._providers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        source_statuses: list[SportsLiveSourceStatus] = []
        games: list[SportsLiveGame] = []
        for (source, _provider), result in zip(self._providers, results, strict=True):
            if isinstance(result, Exception):
                cached = self._provider_cache.get(source)
                if cached is not None:
                    source_statuses.append(_cached_source_status(source, cached, last_error=str(result)))
                    games.extend(cached.games)
                else:
                    source_statuses.append(
                        SportsLiveSourceStatus(
                            source=source,
                            success=False,
                            health=SportsLiveSourceHealth.FAILED,
                            games_seen=0,
                            observed_at=observed_at,
                            last_error=str(result),
                        )
                    )
                continue
            status = _source_status_from_snapshot(source, result)
            source_statuses.append(status)
            if status.success:
                self._provider_cache[source] = result
            games.extend(result.games)
        return SportsLiveSnapshot(
            source="sports_live_aggregate",
            observed_at=observed_at,
            games=_dedupe_games(tuple(games)),
            source_statuses=tuple(source_statuses),
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
    """外层 provider 超时时复用上次成功快照，避免清空全体育直播覆盖。"""

    return SportsLiveSourceStatus(
        source=source,
        success=True,
        health=SportsLiveSourceHealth.CACHED,
        games_seen=len(snapshot.games),
        observed_at=snapshot.observed_at,
        last_error=last_error,
    )


def _dedupe_games(games: tuple[SportsLiveGame, ...]) -> tuple[SportsLiveGame, ...]:
    selected: list[SportsLiveGame] = []
    for game in games:
        duplicate_index = _duplicate_index(selected, game)
        if duplicate_index is None:
            selected.append(game)
            continue
        current = selected[duplicate_index]
        replacement, conflict = _select_game(current, game)
        if conflict is None:
            selected[duplicate_index] = replacement
            continue
        selected[duplicate_index] = _with_source_conflict(replacement, conflict)
    return tuple(selected)


def _source_status_from_snapshot(source: str, snapshot: SportsLiveSnapshot) -> SportsLiveSourceStatus:
    for status in snapshot.source_statuses:
        if status.source == source:
            return replace(status, games_seen=len(snapshot.games), observed_at=status.observed_at or snapshot.observed_at)
    health = (
        SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
        if snapshot.games
        else SportsLiveSourceHealth.SUCCESS_EMPTY
    )
    return SportsLiveSourceStatus(
        source=source,
        success=True,
        health=health,
        games_seen=len(snapshot.games),
        observed_at=snapshot.observed_at,
    )


def _select_game(left: SportsLiveGame, right: SportsLiveGame) -> tuple[SportsLiveGame, SportsLiveGame | None]:
    if _is_official(left.source) and not _is_official(right.source) and left.status != right.status:
        return left, right
    if _is_official(right.source) and not _is_official(left.source) and left.status != right.status:
        return right, left
    if _selection_score(right) > _selection_score(left):
        return right, left if left.status != right.status else None
    return left, right if left.status != right.status else None


def _with_source_conflict(selected: SportsLiveGame, conflict: SportsLiveGame) -> SportsLiveGame:
    raw_conflicts = selected.source_payload.get("source_conflicts")
    existing = raw_conflicts if isinstance(raw_conflicts, tuple) else ()
    return replace(
        selected,
        source_payload={
            **selected.source_payload,
            "source_conflicts": (
                *existing,
                {
                    "source": conflict.source,
                    "status": conflict.status.value,
                    "raw_status": conflict.raw_status,
                },
            ),
        },
    )


def _duplicate_index(selected: Sequence[SportsLiveGame], game: SportsLiveGame) -> int | None:
    for index, current in enumerate(selected):
        if _same_game(current, game):
            return index
    return None


def _same_game(left: SportsLiveGame, right: SportsLiveGame) -> bool:
    if left.league.strip().upper() != right.league.strip().upper():
        return False
    left_bucket = _event_start_bucket(left)
    right_bucket = _event_start_bucket(right)
    if left_bucket is not None and right_bucket is not None and left_bucket != right_bucket:
        return False
    return _teams_match(left.home, right.home) and _teams_match(left.away, right.away)


def _selection_score(game: SportsLiveGame) -> tuple[int, int, float]:
    observed_at = game.observed_at or datetime.fromtimestamp(0, tz=timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return (_STATUS_PRIORITY.get(game.status, 0), _source_priority(game.source), observed_at.timestamp())


def _game_key(game: SportsLiveGame) -> tuple[str, str, str]:
    return (
        game.league.strip().upper(),
        _team_key(game.home.display_name or game.home.name or game.home.abbreviation),
        _team_key(game.away.display_name or game.away.name or game.away.abbreviation),
    )


def _team_key(value: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    return normalized or "unknown"


def _source_priority(source: str) -> int:
    source_code = str(source or "").split(":", maxsplit=1)[0].strip().lower()
    return _SOURCE_PRIORITY.get(source_code, 0)


def _is_official(source: str) -> bool:
    return str(source or "").split(":", maxsplit=1)[0].strip().lower() in _OFFICIAL_SOURCES


def _teams_match(left: Any, right: Any) -> bool:
    left_aliases = _team_alias_keys(left)
    right_aliases = _team_alias_keys(right)
    if left_aliases.intersection(right_aliases):
        return True
    for left_alias in left_aliases:
        for right_alias in right_aliases:
            shorter, longer = sorted((left_alias, right_alias), key=len)
            if len(shorter) >= 4 and longer.endswith(shorter):
                return True
    return False


def _team_alias_keys(team: Any) -> set[str]:
    aliases = team.match_aliases() if hasattr(team, "match_aliases") else ()
    result = {_team_key(alias) for alias in aliases}
    return {alias for alias in result if alias != "unknown"}


def _event_start_bucket(game: SportsLiveGame) -> str | None:
    payload = game.source_payload
    timestamp = _timestamp_value(payload.get("start_timestamp"))
    if timestamp is not None:
        return _bucket_from_datetime(datetime.fromtimestamp(timestamp, tz=timezone.utc))
    for key in ("game_time_utc", "start_time_utc", "game_date", "date", "official_date"):
        value = payload.get(key)
        bucket = _bucket_from_text(value)
        if bucket is not None:
            return bucket
    return None


def _timestamp_value(value: object) -> float | None:
    if value is None:
        return None
    try:
        timestamp = float(str(value))
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    return timestamp


def _bucket_from_text(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _bucket_from_datetime(parsed)


def _bucket_from_datetime(value: datetime) -> str:
    utc_value = value.astimezone(timezone.utc)
    return utc_value.strftime("%Y-%m-%dT%H")
