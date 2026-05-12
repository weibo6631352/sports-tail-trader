"""API-Football (RapidAPI) 足球直播适配器。

文档：https://www.api-football.com/documentation-v3
- ``GET /v3/fixtures?live=all`` 返回所有当前直播 fixtures；
- 鉴权：header ``x-rapidapi-key: <token>``、``x-rapidapi-host: api-football-v1.p.rapidapi.com``；
- payload 顶层 ``{"response": [ { fixture, league, teams, goals, score, ... } ]}``。

token 未配置时 ``enabled=False``，主路径静默跳过装配。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    SoccerGameState,
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

_DEFAULT_BASE_URL = "https://api-football-v1.p.rapidapi.com/v3"


class ApiFootballClient:
    """读取 API-Football live fixtures 并归一化成 ``LiveEvent(sport="football")``。"""

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        api_token: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
        min_fetch_interval_s: float = 20.0,
        max_stale_on_error_s: float = 300.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token.strip() if isinstance(api_token, str) else None
        # rapidapi host 必须与 base_url host 一致才能命中；不可硬编码。
        host = urlsplit(self._base_url).netloc or "api-football-v1.p.rapidapi.com"
        self._rapidapi_host = host
        self._timeout_s = max(0.1, float(timeout_s))
        self._min_fetch_interval_s = max(0.0, float(min_fetch_interval_s))
        self._max_stale_on_error_s = max(0.0, float(max_stale_on_error_s))
        self._now_provider = now_provider
        self._cached_snapshot: SportsLiveSnapshot | None = None
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_s,
            headers=self._build_headers(),
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def enabled(self) -> bool:
        return bool(self._api_token)

    async def list_events(self) -> SportsLiveSnapshot:
        observed_at = utc_now(self._now_provider)
        if not self.enabled:
            return _snapshot_with_status(
                SportsLiveSnapshot(source="api_football", observed_at=observed_at, events=()),
                health=SportsLiveSourceHealth.FAILED,
                success=False,
                last_error="api_football_token_missing",
            )
        if self._is_cache_fresh(observed_at):
            return _snapshot_with_status(
                self._cached_snapshot or SportsLiveSnapshot(source="api_football", observed_at=observed_at, events=()),
                health=SportsLiveSourceHealth.CACHED,
            )
        try:
            payload = await self._get_fixtures()
        except Exception as exc:
            if self._is_cache_usable_after_error(observed_at):
                return _snapshot_with_status(
                    self._cached_snapshot or SportsLiveSnapshot(source="api_football", observed_at=observed_at, events=()),
                    health=SportsLiveSourceHealth.CACHED,
                )
            if isinstance(exc, SportsDataRateLimitError):
                snapshot = _snapshot_with_status(
                    SportsLiveSnapshot(source="api_football", observed_at=observed_at, events=()),
                    health=SportsLiveSourceHealth.RATE_LIMITED,
                    success=False,
                    last_error=str(exc),
                )
                self._cached_snapshot = snapshot
                return snapshot
            raise
        events = parse_api_football_payload(payload, observed_at=observed_at)
        snapshot = SportsLiveSnapshot(
            source="api_football",
            observed_at=observed_at,
            events=events,
        )
        snapshot = _snapshot_with_status(
            snapshot,
            health=(
                SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
                if snapshot.events
                else SportsLiveSourceHealth.SUCCESS_EMPTY
            ),
        )
        self._cached_snapshot = snapshot
        return snapshot

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "user-agent": "sports-tail-trader/0.1",
            "x-rapidapi-host": self._rapidapi_host,
        }
        if self._api_token:
            headers["x-rapidapi-key"] = self._api_token
        return headers

    def _is_cache_fresh(self, observed_at: datetime) -> bool:
        if self._cached_snapshot is None:
            return False
        return _snapshot_age_seconds(self._cached_snapshot, observed_at) < self._min_fetch_interval_s

    def _is_cache_usable_after_error(self, observed_at: datetime) -> bool:
        if self._cached_snapshot is None:
            return False
        return _snapshot_age_seconds(self._cached_snapshot, observed_at) <= self._max_stale_on_error_s

    async def _get_fixtures(self) -> Mapping[str, Any]:
        operation = "api_football_fixtures_live"
        try:
            response = await self._client.get("/fixtures", params={"live": "all"})
            response.raise_for_status()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        return json_mapping_from_response(response, operation=operation)


def parse_api_football_payload(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
) -> tuple[LiveEvent, ...]:
    """把 API-Football fixtures?live=all 转成 ``LiveEvent`` 列表。"""

    observed_at = observed_at or datetime.now(timezone.utc)
    raw = payload.get("response")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    events: list[LiveEvent] = []
    for raw_event in raw:
        if not isinstance(raw_event, Mapping):
            continue
        event = _parse_fixture(raw_event, observed_at=observed_at)
        if event is not None:
            events.append(event)
    return tuple(events)


def _parse_fixture(raw: Mapping[str, Any], *, observed_at: datetime) -> LiveEvent | None:
    fixture = raw.get("fixture") if isinstance(raw.get("fixture"), Mapping) else {}
    league_payload = raw.get("league") if isinstance(raw.get("league"), Mapping) else {}
    teams = raw.get("teams") if isinstance(raw.get("teams"), Mapping) else {}
    goals = raw.get("goals") if isinstance(raw.get("goals"), Mapping) else {}
    home_team = teams.get("home") if isinstance(teams.get("home"), Mapping) else None
    away_team = teams.get("away") if isinstance(teams.get("away"), Mapping) else None
    if not isinstance(home_team, Mapping) or not isinstance(away_team, Mapping):
        return None
    home_name = first_text(home_team, "name")
    away_name = first_text(away_team, "name")
    if home_name is None or away_name is None:
        return None
    home_id = first_text(home_team, "id")
    away_id = first_text(away_team, "id")
    fixture_id = first_text(fixture, "id") or ""
    status_payload = fixture.get("status") if isinstance(fixture.get("status"), Mapping) else {}
    status_short = (first_text(status_payload, "short") or "").upper()
    status = _map_status(status_short)
    clock_minutes = int_value(status_payload.get("elapsed"))
    soccer_state = SoccerGameState(
        period=_period_from_status(status_short),
        clock_minutes=clock_minutes,
        added_minutes=int_value(status_payload.get("extra")),
    )
    league_name = first_text(league_payload, "name") or first_text(league_payload, "id") or "FOOTBALL"
    event_external_ids: dict[str, str] = {}
    if fixture_id:
        event_external_ids["api_football"] = fixture_id
    home_external_ids: dict[str, str] = {}
    if home_id:
        home_external_ids["api_football"] = home_id
    away_external_ids: dict[str, str] = {}
    if away_id:
        away_external_ids["api_football"] = away_id
    return LiveEvent(
        source="api_football",
        source_event_id=fixture_id,
        kind=LiveEventKind.TEAM_MATCH,
        league=league_name,
        sport="football",
        participants=(
            Participant(
                role="home",
                name=home_name,
                display_name=home_name,
                score=int_value(goals.get("home")) or 0,
                external_ids=home_external_ids,
            ),
            Participant(
                role="away",
                name=away_name,
                display_name=away_name,
                score=int_value(goals.get("away")) or 0,
                external_ids=away_external_ids,
            ),
        ),
        status=status,
        period=_period_from_status(status_short),
        seconds_remaining=None,
        observed_at=observed_at,
        event_start_time=_parse_fixture_date(fixture.get("date")),
        event_name=league_name,
        external_ids=event_external_ids,
        raw_status=status_short,
        soccer_state=soccer_state,
        source_payload={
            "sport": "football",
            "league_id": league_payload.get("id"),
            "league_name": league_name,
            "league_country": league_payload.get("country"),
            "season": league_payload.get("season"),
            "round": league_payload.get("round"),
            "start_time_utc": fixture.get("date"),
            "status_short": status_short,
        },
    )


def _map_status(status_short: str) -> SportsLiveGameStatus:
    """API-Football short status code 映射。"""

    if status_short in {"NS", "TBD"}:
        return SportsLiveGameStatus.SCHEDULED
    if status_short in {"1H", "2H", "ET", "P", "BT", "LIVE"}:
        return SportsLiveGameStatus.LIVE
    if status_short in {"HT"}:
        return SportsLiveGameStatus.PAUSED
    if status_short in {"FT", "AET", "PEN"}:
        return SportsLiveGameStatus.ENDED
    if status_short in {"SUSP", "INT"}:
        return SportsLiveGameStatus.PAUSED
    if status_short in {"PST"}:
        return SportsLiveGameStatus.POSTPONED
    if status_short in {"CANC", "ABD"}:
        return SportsLiveGameStatus.CANCELLED
    if status_short in {"AWD", "WO"}:
        return SportsLiveGameStatus.RETIRED
    return SportsLiveGameStatus.UNKNOWN


def _period_from_status(status_short: str) -> str:
    if status_short == "1H":
        return "first_half"
    if status_short == "2H":
        return "second_half"
    if status_short == "ET":
        return "extra_time"
    if status_short == "P":
        return "penalties"
    if status_short == "HT":
        return "halftime"
    return ""


def _parse_fixture_date(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _snapshot_with_status(
    snapshot: SportsLiveSnapshot,
    *,
    health: SportsLiveSourceHealth,
    success: bool = True,
    last_error: str | None = None,
) -> SportsLiveSnapshot:
    return SportsLiveSnapshot(
        source="api_football",
        observed_at=snapshot.observed_at,
        events=snapshot.events,
        source_statuses=(
            SportsLiveSourceStatus(
                source="api_football",
                success=success,
                health=health,
                events_seen=len(snapshot.events),
                observed_at=snapshot.observed_at,
                last_error=last_error,
            ),
        ),
    )


def _snapshot_age_seconds(snapshot: SportsLiveSnapshot, observed_at: datetime) -> float:
    age = observed_at - snapshot.observed_at
    return max(0.0, age.total_seconds())


__all__ = ["ApiFootballClient", "parse_api_football_payload"]
