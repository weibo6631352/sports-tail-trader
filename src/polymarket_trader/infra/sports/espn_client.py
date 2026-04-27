"""ESPN scoreboard REST 适配器。

该模块只负责协议访问和字段归一化，不判断盘口、仓位或是否交易。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from polymarket_trader.domain.events import sanitize_raw_response
from polymarket_trader.domain.sports_live import (
    SportsLiveGame,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
    SportsLiveTeam,
)

_DEFAULT_LEAGUE_PATHS: dict[str, str] = {
    "nba": "/apis/site/v2/sports/basketball/nba/scoreboard",
    "wnba": "/apis/site/v2/sports/basketball/wnba/scoreboard",
    "ncaamb": "/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
    "ncaawb": "/apis/site/v2/sports/basketball/womens-college-basketball/scoreboard",
    "nfl": "/apis/site/v2/sports/football/nfl/scoreboard",
    "ncaaf": "/apis/site/v2/sports/football/college-football/scoreboard",
    "nhl": "/apis/site/v2/sports/hockey/nhl/scoreboard",
    "mlb": "/apis/site/v2/sports/baseball/mlb/scoreboard",
}

_DEFAULT_SCOREBOARD_TIMEZONE = "America/New_York"

_LEAGUE_SECONDS = {
    "nba": (4, 12 * 60),
    "wnba": (4, 10 * 60),
    "ncaamb": (2, 20 * 60),
    "ncaawb": (4, 10 * 60),
    "nfl": (4, 15 * 60),
    "ncaaf": (4, 15 * 60),
    "nhl": (3, 20 * 60),
}


class SportsDataClientError(RuntimeError):
    """外部体育数据源错误基类。"""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        url: str | None = None,
        status_code: int | None = None,
        code: str | None = None,
        retry_after_s: float | None = None,
        raw_response_summary: str | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.url = url
        self.status_code = status_code
        self.code = code
        self.retry_after_s = retry_after_s
        self.raw_response_summary = raw_response_summary

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "operation": self.operation,
            "url": self.url,
            "status_code": self.status_code,
            "code": self.code,
            "retry_after_s": self.retry_after_s,
            "raw_response_summary": self.raw_response_summary,
        }


class SportsDataTimeoutError(SportsDataClientError):
    """外部体育数据源请求超时。"""


class SportsDataRateLimitError(SportsDataClientError):
    """外部体育数据源限流。"""


class SportsDataTransportError(SportsDataClientError):
    """外部体育数据源网络或服务端错误。"""


class SportsDataResponseError(SportsDataClientError):
    """外部体育数据源响应格式错误。"""


class EspnScoreboardClient:
    """读取 ESPN scoreboard 并转换成内部体育直播 DTO。"""

    def __init__(
        self,
        *,
        base_url: str = "https://site.api.espn.com",
        leagues: Sequence[str] = ("nba", "nhl", "nfl", "mlb"),
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
        limit: int = 100,
        date_window_days_before: int = 0,
        date_window_days_after: int = 0,
        scoreboard_timezone: str = _DEFAULT_SCOREBOARD_TIMEZONE,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._leagues = tuple(_normalize_league_code(league) for league in leagues if str(league).strip())
        self._limit = max(1, limit)
        self._date_window_days_before = max(0, int(date_window_days_before))
        self._date_window_days_after = max(0, int(date_window_days_after))
        self._scoreboard_timezone = _timezone(scoreboard_timezone)
        self._now_provider = now_provider
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_s,
            headers={"accept": "application/json"},
            trust_env=False,
        )

    @property
    def leagues(self) -> tuple[str, ...]:
        return self._leagues

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def list_games(self) -> SportsLiveSnapshot:
        """拉取所有配置联赛的当前 scoreboard。"""

        observed_at = self._now()
        scoreboard_dates = _scoreboard_dates(
            observed_at,
            timezone_=self._scoreboard_timezone,
            days_before=self._date_window_days_before,
            days_after=self._date_window_days_after,
        )
        games: list[SportsLiveGame] = []
        seen_event_keys: set[tuple[str, str]] = set()
        for league in self._leagues:
            for scoreboard_date in scoreboard_dates:
                payload = await self._get_scoreboard(league, scoreboard_date=scoreboard_date)
                for game in parse_espn_scoreboard_payload(
                    payload,
                    league=league,
                    observed_at=observed_at,
                ):
                    event_key = (game.league, game.source_event_id)
                    if game.source_event_id and event_key in seen_event_keys:
                        continue
                    seen_event_keys.add(event_key)
                    games.append(game)
        return SportsLiveSnapshot(source="espn", observed_at=observed_at, games=tuple(games))

    async def _get_scoreboard(self, league: str, *, scoreboard_date: str) -> Mapping[str, Any]:
        path = _path_for_league(league)
        operation = f"espn_scoreboard:{league}"
        try:
            response = await self._client.get(
                path,
                params={"limit": self._limit, "dates": scoreboard_date},
            )
            response.raise_for_status()
        except Exception as exc:
            raise _normalize_error(exc, operation=operation) from exc
        try:
            payload = response.json()
        except Exception as exc:
            raise SportsDataResponseError(
                f"{operation} returned non-JSON payload",
                operation=operation,
                url=str(response.request.url),
                status_code=response.status_code,
                raw_response_summary=sanitize_raw_response(response.text, max_length=512),
            ) from exc
        if not isinstance(payload, Mapping):
            raise SportsDataResponseError(
                f"{operation} returned unexpected payload",
                operation=operation,
                url=str(response.request.url),
                status_code=response.status_code,
                raw_response_summary=sanitize_raw_response(payload, max_length=512),
            )
        return payload

    def _now(self) -> datetime:
        value = (
            datetime.now(timezone.utc)
            if self._now_provider is None
            else self._now_provider()
        )
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def parse_espn_scoreboard_payload(
    payload: Mapping[str, Any],
    *,
    league: str,
    observed_at: datetime | None = None,
) -> tuple[SportsLiveGame, ...]:
    """把 ESPN scoreboard 原始 payload 转成内部直播比赛 DTO。

    该函数供运行时 client 和离线真实样本校验复用。它不发起网络请求，也不判断
    盘口或交易机会，便于把采集到的 ESPN JSON 固定成回归样本。
    """

    observed_at = observed_at or datetime.now(timezone.utc)
    league = _normalize_league_code(league)
    events = payload.get("events")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        return ()
    games: list[SportsLiveGame] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        game = _parse_event(event, league=league, observed_at=observed_at)
        if game is not None:
            games.append(game)
    return tuple(games)


def _parse_event(
    event: Mapping[str, Any],
    *,
    league: str,
    observed_at: datetime,
) -> SportsLiveGame | None:
    competitions = event.get("competitions")
    if not isinstance(competitions, Sequence) or not competitions:
        return None
    competition = competitions[0]
    if not isinstance(competition, Mapping):
        return None
    competitors = competition.get("competitors")
    if not isinstance(competitors, Sequence):
        return None
    home = _team_from_competitors(competitors, home_away="home")
    away = _team_from_competitors(competitors, home_away="away")
    if home is None or away is None:
        return None

    status_payload = event.get("status")
    if not isinstance(status_payload, Mapping):
        status_payload = competition.get("status")
    if not isinstance(status_payload, Mapping):
        status_payload = {}
    raw_status = _status_name(status_payload)
    status = _map_status(status_payload)
    period = _period_label(league, _int_value(status_payload.get("period")), raw_status)
    seconds_remaining = _seconds_remaining(
        league=league,
        status_payload=status_payload,
        normalized_status=status,
    )

    return SportsLiveGame(
        source="espn",
        source_event_id=str(event.get("id") or competition.get("id") or ""),
        league=league.upper(),
        home=home,
        away=away,
        status=status,
        period=period,
        seconds_remaining=seconds_remaining,
        observed_at=observed_at,
        raw_status=raw_status,
        source_payload={
            "event_id": event.get("id"),
            "name": event.get("name"),
            "short_name": event.get("shortName"),
        },
    )


def _team_from_competitors(
    competitors: Sequence[Any],
    *,
    home_away: str,
) -> SportsLiveTeam | None:
    for item in competitors:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("homeAway") or "").lower() != home_away:
            continue
        team = item.get("team")
        team_payload = team if isinstance(team, Mapping) else {}
        name = _first_text(
            team_payload,
            "name",
            "displayName",
            "shortDisplayName",
            "abbreviation",
        ) or home_away
        return SportsLiveTeam(
            name=name,
            score=_int_value(item.get("score")) or 0,
            display_name=_first_text(team_payload, "displayName"),
            abbreviation=_first_text(team_payload, "abbreviation"),
            short_name=_first_text(team_payload, "shortDisplayName"),
            location=_first_text(team_payload, "location"),
            aliases=tuple(
                value
                for value in (
                    _first_text(item, "displayName"),
                    _first_text(item, "shortDisplayName"),
                )
                if value
            ),
        )
    return None


def _map_status(status_payload: Mapping[str, Any]) -> SportsLiveGameStatus:
    status_type = status_payload.get("type")
    type_payload = status_type if isinstance(status_type, Mapping) else {}
    state = str(type_payload.get("state") or "").lower()
    name = str(type_payload.get("name") or "").upper()
    detail = " ".join(
        str(value).lower()
        for value in (
            type_payload.get("detail"),
            type_payload.get("shortDetail"),
            type_payload.get("description"),
        )
        if value
    )
    if name in {"STATUS_POSTPONED"}:
        return SportsLiveGameStatus.POSTPONED
    if name in {"STATUS_CANCELED", "STATUS_CANCELLED"}:
        return SportsLiveGameStatus.CANCELLED
    if name in {"STATUS_SUSPENDED", "STATUS_DELAYED"}:
        return SportsLiveGameStatus.PAUSED
    if state == "in":
        if "halftime" in detail or "intermission" in detail:
            return SportsLiveGameStatus.PAUSED
        return SportsLiveGameStatus.LIVE
    if state == "pre":
        return SportsLiveGameStatus.SCHEDULED
    if state == "post":
        return SportsLiveGameStatus.ENDED
    return SportsLiveGameStatus.UNKNOWN


def _seconds_remaining(
    *,
    league: str,
    status_payload: Mapping[str, Any],
    normalized_status: SportsLiveGameStatus,
) -> int | None:
    if normalized_status not in {SportsLiveGameStatus.LIVE, SportsLiveGameStatus.PAUSED}:
        return None
    period_count, period_seconds = _LEAGUE_SECONDS.get(league, (0, 0))
    if period_count <= 0 or period_seconds <= 0:
        return None
    period = _int_value(status_payload.get("period")) or 0
    if period <= 0:
        return None
    clock_remaining = _clock_seconds(status_payload.get("displayClock"))
    if clock_remaining is None:
        clock_remaining = _int_value(status_payload.get("clock"))
    if clock_remaining is None:
        return None
    completed_periods = max(0, min(period - 1, period_count))
    future_periods = max(0, period_count - completed_periods - 1)
    return max(0, int(clock_remaining) + future_periods * period_seconds)


def _period_label(league: str, period: int | None, raw_status: str | None) -> str:
    if period is None or period <= 0:
        return raw_status or ""
    if league in {"nba", "wnba", "nfl", "ncaaf"}:
        return f"Q{period}"
    if league in {"ncaamb"}:
        return f"H{period}"
    if league == "nhl":
        return f"P{period}"
    if league == "mlb":
        return f"I{period}"
    return str(period)


def _status_name(status_payload: Mapping[str, Any]) -> str | None:
    status_type = status_payload.get("type")
    type_payload = status_type if isinstance(status_type, Mapping) else {}
    return _first_text(type_payload, "name", "description", "detail", "shortDetail")


def _clock_seconds(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(float(parts[1]))
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _path_for_league(league: str) -> str:
    normalized = _normalize_league_code(league)
    if normalized.startswith("soccer:"):
        soccer_league = normalized.split(":", 1)[1]
        return f"/apis/site/v2/sports/soccer/{soccer_league}/scoreboard"
    path = _DEFAULT_LEAGUE_PATHS.get(normalized)
    if path is None:
        raise SportsDataResponseError(
            f"unsupported sports league: {league}",
            operation="espn_scoreboard",
            code="unsupported_league",
        )
    return path


def _normalize_league_code(value: Any) -> str:
    return str(value).strip().lower()


def _timezone(value: str) -> tzinfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _scoreboard_dates(
    observed_at: datetime,
    *,
    timezone_: tzinfo,
    days_before: int,
    days_after: int,
) -> tuple[str, ...]:
    local_date = observed_at.astimezone(timezone_).date()
    return tuple(
        (local_date + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(-days_before, days_after + 1)
    )


def _normalize_error(exc: Exception, *, operation: str) -> SportsDataClientError:
    if isinstance(exc, SportsDataClientError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return SportsDataTimeoutError(f"{operation} timed out", operation=operation)
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        retry_after_s: float | None = None
        try:
            retry_after = response.headers.get("retry-after")
            retry_after_s = None if retry_after is None else float(retry_after)
        except (TypeError, ValueError):
            retry_after_s = None
        payload: Any
        try:
            payload = response.json()
        except Exception:
            payload = response.text
        error_cls = (
            SportsDataRateLimitError
            if response.status_code == 429
            else SportsDataTransportError if response.status_code >= 500 else SportsDataResponseError
        )
        return error_cls(
            f"{operation} failed with HTTP {response.status_code}",
            operation=operation,
            url=str(response.request.url),
            status_code=response.status_code,
            retry_after_s=retry_after_s,
            raw_response_summary=sanitize_raw_response(payload, max_length=512),
        )
    if isinstance(exc, httpx.HTTPError):
        return SportsDataTransportError(f"{operation} transport error: {exc}", operation=operation)
    return SportsDataResponseError(f"{operation} failed: {exc}", operation=operation)


def _int_value(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _first_text(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None
