"""TheSportsDB 公开赛程源适配器。

TheSportsDB 的免费 eventsday API 能提供部分联赛的当日赛程、比分和粗粒度状态。
本适配器只把可验证字段转换成内部 ``LiveEvent``，不推断剩余秒数，也不把国家或
场馆字段加入队伍匹配别名，避免把冠军归属、国家归属市场误配成具体球队比赛。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import re
from typing import Any

import httpx

from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
    SportsLiveSourceStatus,
)
from polymarket_trader.infra.sports.common import (
    SportsDataRateLimitError,
    first_text,
    int_value,
    json_mapping_from_response,
    normalize_sports_data_error,
    utc_now,
)

_DEFAULT_BASE_URL = "https://www.thesportsdb.com/api/v1/json/3"
_EVENTS_DAY_PATH = "/eventsday.php"

_SPORTS_BY_LEAGUE: dict[str, tuple[str, ...]] = {
    "nhl": ("Ice Hockey",),
    "ice-hockey": ("Ice Hockey",),
    "hockey": ("Ice Hockey",),
    "mlb": ("Baseball",),
    "baseball": ("Baseball",),
}

_LEAGUE_ALIASES: dict[str, tuple[str, ...] | None] = {
    "nhl": ("nhl", "national hockey league"),
    "ice-hockey": None,
    "hockey": None,
    "mlb": ("mlb", "major league baseball"),
    "baseball": None,
}

# TheSportsDB strSport → 内部 sport 命名空间（与 espn / sofascore 一致）。
_SPORT_NAMESPACE: dict[str, str] = {
    "ice hockey": "ice-hockey",
    "hockey": "ice-hockey",
    "baseball": "baseball",
    "basketball": "basketball",
    "american football": "american-football",
    "football": "football",
    "soccer": "football",
    "tennis": "tennis",
    "cricket": "cricket",
    "rugby": "rugby",
    "rugby union": "rugby",
    "rugby league": "rugby",
    "esports": "esports",
    "mma": "mma",
    "boxing": "boxing",
}


class TheSportsDbLiveClient:
    """读取 TheSportsDB eventsday API 并归一化成 ``LiveEvent`` 列表。"""

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        sports: Sequence[str] | None = None,
        league_codes: Sequence[str] = ("nba", "nhl", "nfl", "mlb"),
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
        min_fetch_interval_s: float = 60.0,
        max_stale_on_error_s: float = 300.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._league_codes = _normalize_codes(league_codes)
        self._sports = _normalize_sports(sports or thesportsdb_sports_for_leagues(self._league_codes))
        self._now_provider = now_provider
        self._min_fetch_interval_s = max(0.0, float(min_fetch_interval_s))
        self._max_stale_on_error_s = max(0.0, float(max_stale_on_error_s))
        self._cached_snapshot: SportsLiveSnapshot | None = None
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_s,
            headers={
                "accept": "application/json",
                "user-agent": "sports-tail-trader/0.1",
            },
            trust_env=False,
        )

    @property
    def sports(self) -> tuple[str, ...]:
        """返回当前 client 会拉取的 TheSportsDB sport 名称。"""

        return self._sports

    async def aclose(self) -> None:
        """关闭内部 HTTP client。"""

        if self._owns_client:
            await self._client.aclose()

    async def list_events(self) -> SportsLiveSnapshot:
        """拉取配置 sport 的 UTC 当前日赛程。"""

        observed_at = utc_now(self._now_provider)
        if self._is_cache_fresh(observed_at):
            return self._snapshot_with_status(
                self._cached_snapshot or SportsLiveSnapshot(
                    source="thesportsdb",
                    observed_at=observed_at,
                    events=(),
                ),
                health=SportsLiveSourceHealth.CACHED,
            )
        date_text = observed_at.strftime("%Y-%m-%d")
        events: list[LiveEvent] = []
        try:
            for sport in self._sports:
                payload = await self._get_eventsday(sport, date_text=date_text)
                events.extend(
                    parse_thesportsdb_events_payload(
                        payload,
                        sport=sport,
                        league_codes=self._league_codes,
                        observed_at=observed_at,
                    )
                )
        except Exception as exc:
            if self._is_cache_usable_after_error(observed_at):
                return self._snapshot_with_status(
                    self._cached_snapshot or SportsLiveSnapshot(
                        source="thesportsdb",
                        observed_at=observed_at,
                        events=(),
                    ),
                    health=SportsLiveSourceHealth.CACHED,
                )
            if isinstance(exc, SportsDataRateLimitError):
                snapshot = self._snapshot_with_status(
                    SportsLiveSnapshot(source="thesportsdb", observed_at=observed_at, events=()),
                    health=SportsLiveSourceHealth.RATE_LIMITED,
                    success=False,
                    last_error=str(exc),
                )
                self._cached_snapshot = snapshot
                return snapshot
            raise
        snapshot = SportsLiveSnapshot(source="thesportsdb", observed_at=observed_at, events=tuple(events))
        snapshot = self._snapshot_with_status(
            snapshot,
            health=(
                SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
                if snapshot.events
                else SportsLiveSourceHealth.SUCCESS_EMPTY
            ),
        )
        self._cached_snapshot = snapshot
        return snapshot

    def _is_cache_fresh(self, observed_at: datetime) -> bool:
        if self._cached_snapshot is None:
            return False
        return _snapshot_age_seconds(self._cached_snapshot, observed_at) < self._min_fetch_interval_s

    def _is_cache_usable_after_error(self, observed_at: datetime) -> bool:
        if self._cached_snapshot is None:
            return False
        return _snapshot_age_seconds(self._cached_snapshot, observed_at) <= self._max_stale_on_error_s

    def _snapshot_with_status(
        self,
        snapshot: SportsLiveSnapshot,
        *,
        health: SportsLiveSourceHealth,
        success: bool = True,
        last_error: str | None = None,
    ) -> SportsLiveSnapshot:
        return SportsLiveSnapshot(
            source="thesportsdb",
            observed_at=snapshot.observed_at,
            events=snapshot.events,
            source_statuses=(
                SportsLiveSourceStatus(
                    source="thesportsdb",
                    success=success,
                    health=health,
                    events_seen=len(snapshot.events),
                    observed_at=snapshot.observed_at,
                    last_error=last_error,
                ),
            ),
        )

    async def _get_eventsday(self, sport: str, *, date_text: str) -> Mapping[str, Any]:
        operation = f"thesportsdb_eventsday:{sport}"
        try:
            response = await self._client.get(
                _EVENTS_DAY_PATH,
                params={"d": date_text, "s": sport},
            )
            response.raise_for_status()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        return json_mapping_from_response(response, operation=operation)


def thesportsdb_sports_for_leagues(league_codes: Sequence[str]) -> tuple[str, ...]:
    """按内部联赛代码返回 TheSportsDB sport 名称。"""

    seen: set[str] = set()
    result: list[str] = []
    for code in _normalize_codes(league_codes):
        for sport in _SPORTS_BY_LEAGUE.get(code, ()):
            if sport in seen:
                continue
            seen.add(sport)
            result.append(sport)
    return tuple(result)


def parse_thesportsdb_events_payload(
    payload: Mapping[str, Any],
    *,
    sport: str,
    league_codes: Sequence[str] = (),
    observed_at: datetime | None = None,
) -> tuple[LiveEvent, ...]:
    """把 TheSportsDB eventsday payload 转成 ``LiveEvent`` 列表。"""

    observed_at = observed_at or datetime.now(timezone.utc)
    raw_events = payload.get("events")
    if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
        return ()
    allowed_aliases = _allowed_league_aliases(league_codes)
    events: list[LiveEvent] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, Mapping):
            continue
        if not _event_matches_leagues(raw_event, allowed_aliases):
            continue
        event = _parse_event(raw_event, sport=sport, observed_at=observed_at)
        if event is not None:
            events.append(event)
    return tuple(events)


def _parse_event(raw_event: Mapping[str, Any], *, sport: str, observed_at: datetime) -> LiveEvent | None:
    home_name = first_text(raw_event, "strHomeTeam")
    away_name = first_text(raw_event, "strAwayTeam")
    if home_name is None or away_name is None:
        return None
    raw_status = first_text(raw_event, "strStatus", "strProgress") or ""
    status = _map_status(raw_status)
    event_id = first_text(raw_event, "idEvent") or ""
    home_id = first_text(raw_event, "idHomeTeam")
    away_id = first_text(raw_event, "idAwayTeam")
    league_text = first_text(raw_event, "strLeague") or sport
    sport_text = first_text(raw_event, "strSport") or sport
    event_start_time = _parse_event_datetime(raw_event)
    return LiveEvent(
        source="thesportsdb",
        source_event_id=event_id,
        kind=LiveEventKind.TEAM_MATCH,
        league=_league_name(raw_event, sport=sport),
        sport=_sport_namespace(sport_text),
        participants=(
            Participant(
                role="home",
                name=home_name,
                score=int_value(raw_event.get("intHomeScore")) or 0,
                display_name=home_name,
                external_ids={"thesportsdb": home_id} if home_id else {},
            ),
            Participant(
                role="away",
                name=away_name,
                score=int_value(raw_event.get("intAwayScore")) or 0,
                display_name=away_name,
                external_ids={"thesportsdb": away_id} if away_id else {},
            ),
        ),
        status=status,
        period=_period_label(raw_status),
        seconds_remaining=None,
        observed_at=observed_at,
        event_start_time=event_start_time,
        event_name=first_text(raw_event, "strEvent") or "",
        external_ids={"thesportsdb": event_id} if event_id else {},
        raw_status=raw_status,
        source_payload={
            "sport": _sport_namespace(sport_text),
            "league": league_text,
            "league_id": raw_event.get("idLeague"),
            "event_slug": raw_event.get("strEvent"),
            "start_time_utc": raw_event.get("strTimestamp"),
            "date": raw_event.get("dateEvent"),
        },
    )


def _map_status(raw_status: str) -> SportsLiveGameStatus:
    text = _normalize_alias(raw_status)
    if not text:
        return SportsLiveGameStatus.UNKNOWN
    if any(token in text for token in ("postponed", "postp")):
        return SportsLiveGameStatus.POSTPONED
    if "cancel" in text or text in {"can", "cnl"}:
        return SportsLiveGameStatus.CANCELLED
    if any(token in text for token in ("suspend", "delay")):
        return SportsLiveGameStatus.PAUSED
    if text in {"ns", "not started", "tbd"}:
        return SportsLiveGameStatus.SCHEDULED
    if text in {"ft", "final", "finished", "match finished", "ended", "aet", "ap"}:
        return SportsLiveGameStatus.ENDED
    if text in {"ht", "halftime", "intermission"}:
        return SportsLiveGameStatus.PAUSED
    if re.fullmatch(r"(p|q|in)\s*\d+", text) or text in {"live", "ot", "so"}:
        return SportsLiveGameStatus.LIVE
    return SportsLiveGameStatus.UNKNOWN


def _period_label(raw_status: str) -> str:
    text = raw_status.strip()
    normalized = text.upper().replace(" ", "")
    inning = re.fullmatch(r"IN(\d+)", normalized)
    if inning:
        return f"I{inning.group(1)}"
    period = re.fullmatch(r"([PQ])(\d+)", normalized)
    if period:
        return f"{period.group(1)}{period.group(2)}"
    return text


def _league_name(raw_event: Mapping[str, Any], *, sport: str) -> str:
    name = first_text(raw_event, "strLeague") or sport
    normalized = name.strip()
    if len(normalized) <= 5 and re.fullmatch(r"[A-Za-z0-9 ._-]+", normalized):
        return normalized.upper().replace("_", "-")
    return normalized


def _sport_namespace(sport_text: str | None) -> str:
    if not sport_text:
        return ""
    key = sport_text.strip().lower()
    return _SPORT_NAMESPACE.get(key, key.replace(" ", "-"))


def _allowed_league_aliases(league_codes: Sequence[str]) -> set[str] | None:
    codes = _normalize_codes(league_codes)
    if not codes:
        return None
    aliases: set[str] = set()
    has_wildcard = False
    for code in codes:
        values = _LEAGUE_ALIASES.get(code)
        if values is None and code in _LEAGUE_ALIASES:
            has_wildcard = True
            continue
        for value in values or ():
            aliases.add(_normalize_alias(value))
    if has_wildcard:
        return None
    return aliases


def _event_matches_leagues(raw_event: Mapping[str, Any], allowed_aliases: set[str] | None) -> bool:
    if allowed_aliases is None:
        return True
    aliases = {
        _normalize_alias(value)
        for value in (
            first_text(raw_event, "strLeague"),
            first_text(raw_event, "strLeagueAlternate"),
        )
        if value
    }
    return bool(aliases.intersection(allowed_aliases))


def _normalize_codes(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        code = str(value).strip().lower()
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return tuple(result)


def _normalize_sports(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        sport = str(value).strip()
        key = sport.lower()
        if not sport or key in seen:
            continue
        seen.add(key)
        result.append(sport)
    return tuple(result)


def _normalize_alias(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _parse_event_datetime(raw_event: Mapping[str, Any]) -> datetime | None:
    text = first_text(raw_event, "strTimestamp")
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    date_text = first_text(raw_event, "dateEvent")
    time_text = first_text(raw_event, "strTime")
    if date_text:
        candidate = f"{date_text}T{time_text or '00:00:00'}".replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _snapshot_age_seconds(snapshot: SportsLiveSnapshot, observed_at: datetime) -> float:
    age = observed_at - snapshot.observed_at
    return max(0.0, age.total_seconds())
