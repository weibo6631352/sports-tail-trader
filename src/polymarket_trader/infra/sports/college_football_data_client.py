"""College football data adapter（CFBD NCAAF + ncaa-api NCAAB）。

NCAAF：CollegeFootballData ``/games?year=...&seasonType=...``，token 必填（Tier 1+）。
NCAAB：ncaa-api ``/scoreboard/basketball-men/d1/<year>/<week>`` 公共实例，无 auth。

一个 client 内按 sport 分支：football → CFBD，basketball → ncaa-api。CFBD token
未配置时 NCAAF 部分静默跳过，NCAAB 仍能拉。enabled 表示"至少有一项能拉"。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
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
    first_text,
    int_value,
    json_mapping_from_response,
    normalize_sports_data_error,
    utc_now,
)

_DEFAULT_CFBD_BASE_URL = "https://api.collegefootballdata.com"
_DEFAULT_NCAA_API_BASE_URL = "https://ncaa-api.henrygd.me"


class CollegeFootballDataClient:
    """CFBD + ncaa-api 合并 client，分别覆盖 NCAAF 与 NCAAB。

    source name 永远是 ``college_football_data``；内部用 ``external_ids`` 标识
    ``college_football_data:<id>`` 或 ``ncaa:<id>`` 区分子源。
    """

    def __init__(
        self,
        *,
        cfbd_base_url: str = _DEFAULT_CFBD_BASE_URL,
        ncaa_api_base_url: str = _DEFAULT_NCAA_API_BASE_URL,
        cfbd_token: str | None = None,
        cfbd_year: int | None = None,
        cfbd_season_type: str = "regular",
        ncaa_year: int | None = None,
        ncaa_week: str | None = None,
        sports: Sequence[str] = ("football", "basketball"),
        client: httpx.AsyncClient | None = None,
        ncaa_client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._cfbd_base_url = cfbd_base_url.rstrip("/")
        self._ncaa_api_base_url = ncaa_api_base_url.rstrip("/")
        self._cfbd_token = cfbd_token.strip() if isinstance(cfbd_token, str) else None
        self._cfbd_year = cfbd_year
        self._cfbd_season_type = cfbd_season_type
        self._ncaa_year = ncaa_year
        self._ncaa_week = ncaa_week
        self._sports = tuple(str(s).strip().lower() for s in sports if str(s).strip())
        self._timeout_s = max(0.1, float(timeout_s))
        self._now_provider = now_provider
        self._owns_cfbd_client = client is None
        self._cfbd_client = client or httpx.AsyncClient(
            base_url=self._cfbd_base_url,
            timeout=self._timeout_s,
            headers=self._cfbd_headers(),
            trust_env=False,
        )
        self._owns_ncaa_client = ncaa_client is None
        self._ncaa_client = ncaa_client or httpx.AsyncClient(
            base_url=self._ncaa_api_base_url,
            timeout=self._timeout_s,
            headers={"accept": "application/json", "user-agent": "sports-tail-trader/0.1"},
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_cfbd_client:
            await self._cfbd_client.aclose()
        if self._owns_ncaa_client:
            await self._ncaa_client.aclose()

    @property
    def enabled(self) -> bool:
        """只要任一子源可用就视为启用。"""

        if "basketball" in self._sports:
            return True  # ncaa-api 公共实例无需 token
        return bool(self._cfbd_token)

    async def list_events(self) -> SportsLiveSnapshot:
        observed_at = utc_now(self._now_provider)
        events: list[LiveEvent] = []
        statuses: list[SportsLiveSourceStatus] = []
        # NCAAF via CFBD
        if "football" in self._sports:
            if not self._cfbd_token:
                statuses.append(
                    SportsLiveSourceStatus(
                        source="college_football_data",
                        success=False,
                        health=SportsLiveSourceHealth.FAILED,
                        events_seen=0,
                        observed_at=observed_at,
                        last_error="cfbd_token_missing",
                    )
                )
            else:
                try:
                    payload = await self._get_cfbd_games(observed_at)
                except Exception as exc:
                    raw_error = str(exc)
                    statuses.append(
                        SportsLiveSourceStatus(
                            source="college_football_data",
                            success=False,
                            health=SportsLiveSourceHealth.FAILED,
                            events_seen=0,
                            observed_at=observed_at,
                            last_error=raw_error,
                        )
                    )
                else:
                    cfbd_events = parse_cfbd_games_payload(payload, observed_at=observed_at)
                    events.extend(cfbd_events)
                    statuses.append(
                        SportsLiveSourceStatus(
                            source="college_football_data",
                            success=True,
                            health=(
                                SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
                                if cfbd_events
                                else SportsLiveSourceHealth.SUCCESS_EMPTY
                            ),
                            events_seen=len(cfbd_events),
                            observed_at=observed_at,
                        )
                    )
        # NCAAB via ncaa-api
        if "basketball" in self._sports:
            try:
                payload = await self._get_ncaa_basketball(observed_at)
            except Exception as exc:
                raw_error = str(exc)
                statuses.append(
                    SportsLiveSourceStatus(
                        source="ncaa_api",
                        success=False,
                        health=SportsLiveSourceHealth.FAILED,
                        events_seen=0,
                        observed_at=observed_at,
                        last_error=raw_error,
                    )
                )
            else:
                ncaab_events = parse_ncaa_api_scoreboard(payload, observed_at=observed_at)
                events.extend(ncaab_events)
                statuses.append(
                    SportsLiveSourceStatus(
                        source="ncaa_api",
                        success=True,
                        health=(
                            SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
                            if ncaab_events
                            else SportsLiveSourceHealth.SUCCESS_EMPTY
                        ),
                        events_seen=len(ncaab_events),
                        observed_at=observed_at,
                    )
                )
        return SportsLiveSnapshot(
            source="college_football_data",
            observed_at=observed_at,
            events=tuple(events),
            source_statuses=tuple(statuses),
        )

    def _cfbd_headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "user-agent": "sports-tail-trader/0.1",
        }
        if self._cfbd_token:
            headers["authorization"] = f"Bearer {self._cfbd_token}"
        return headers

    async def _get_cfbd_games(self, observed_at: datetime) -> Mapping[str, Any] | Sequence[Any]:
        operation = "cfbd_games"
        params: dict[str, Any] = {
            "year": self._cfbd_year or observed_at.year,
            "seasonType": self._cfbd_season_type,
        }
        try:
            response = await self._cfbd_client.get("/games", params=params)
            response.raise_for_status()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        try:
            data = response.json()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        if isinstance(data, list):
            return data
        if isinstance(data, Mapping):
            return data
        return ()

    async def _get_ncaa_basketball(self, observed_at: datetime) -> Mapping[str, Any]:
        operation = "ncaa_api_basketball_scoreboard"
        year = self._ncaa_year or observed_at.year
        week = self._ncaa_week or observed_at.strftime("%m/%d")
        path = f"/scoreboard/basketball-men/d1/{year}/{week}"
        try:
            response = await self._ncaa_client.get(path)
            response.raise_for_status()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        return json_mapping_from_response(response, operation=operation)


def parse_cfbd_games_payload(
    payload: Any,
    *,
    observed_at: datetime | None = None,
) -> tuple[LiveEvent, ...]:
    """把 CFBD ``/games`` payload 转成 ``LiveEvent`` 序列。

    CFBD games 主要面向历史 + 进行中赛事；本 client 不区分 live/upcoming，
    交给 aggregate 按 status 与 event_start_time 决定融合行为。
    """

    observed_at = observed_at or datetime.now(timezone.utc)
    if isinstance(payload, Mapping):
        items: Sequence[Any] = list(payload.get("games") or payload.get("response") or ())
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        items = payload
    else:
        return ()
    events: list[LiveEvent] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        event = _parse_cfbd_game(raw, observed_at=observed_at)
        if event is not None:
            events.append(event)
    return tuple(events)


def _parse_cfbd_game(raw: Mapping[str, Any], *, observed_at: datetime) -> LiveEvent | None:
    home_name = first_text(raw, "home_team") or first_text(raw, "homeTeam")
    away_name = first_text(raw, "away_team") or first_text(raw, "awayTeam")
    if home_name is None or away_name is None:
        return None
    home_id = first_text(raw, "home_id") or first_text(raw, "homeId")
    away_id = first_text(raw, "away_id") or first_text(raw, "awayId")
    game_id = first_text(raw, "id") or ""
    completed = bool(raw.get("completed"))
    status: SportsLiveGameStatus = (
        SportsLiveGameStatus.ENDED
        if completed
        else SportsLiveGameStatus.SCHEDULED
    )
    home_score = int_value(raw.get("home_points") or raw.get("homePoints"))
    away_score = int_value(raw.get("away_points") or raw.get("awayPoints"))
    if (home_score is not None or away_score is not None) and not completed:
        status = SportsLiveGameStatus.LIVE
    start_text = first_text(raw, "start_date") or first_text(raw, "startDate")
    event_external_ids: dict[str, str] = {}
    if game_id:
        event_external_ids["college_football_data"] = game_id
    home_external_ids: dict[str, str] = {}
    if home_id:
        home_external_ids["college_football_data"] = home_id
    away_external_ids: dict[str, str] = {}
    if away_id:
        away_external_ids["college_football_data"] = away_id
    return LiveEvent(
        source="college_football_data",
        source_event_id=game_id,
        kind=LiveEventKind.TEAM_MATCH,
        league="NCAAF",
        sport="american-football",
        participants=(
            Participant(
                role="home",
                name=home_name,
                display_name=home_name,
                score=home_score or 0,
                external_ids=home_external_ids,
            ),
            Participant(
                role="away",
                name=away_name,
                display_name=away_name,
                score=away_score or 0,
                external_ids=away_external_ids,
            ),
        ),
        status=status,
        observed_at=observed_at,
        event_start_time=_parse_iso(start_text),
        external_ids=event_external_ids,
        raw_status="completed" if completed else "",
        source_payload={
            "sport": "american-football",
            "season": raw.get("season"),
            "week": raw.get("week"),
            "season_type": raw.get("season_type") or raw.get("seasonType"),
            "venue": raw.get("venue"),
            "neutral_site": raw.get("neutral_site") or raw.get("neutralSite"),
            "conference_game": raw.get("conference_game") or raw.get("conferenceGame"),
            "start_time_utc": start_text,
        },
    )


def parse_ncaa_api_scoreboard(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
) -> tuple[LiveEvent, ...]:
    """把 ncaa-api ``/scoreboard/basketball-men/d1/...`` 转成 ``LiveEvent`` 序列。

    ncaa-api 顶层 ``{"games": [ { "game": { ...home, ...away, gameState, ... } } ]}``，
    home/away 各自含 ``names.short``、``names.full``、``score`` 等字段。
    """

    observed_at = observed_at or datetime.now(timezone.utc)
    games = payload.get("games")
    if not isinstance(games, Sequence) or isinstance(games, (str, bytes)):
        return ()
    events: list[LiveEvent] = []
    for wrapper in games:
        if not isinstance(wrapper, Mapping):
            continue
        raw_game = wrapper.get("game") if isinstance(wrapper.get("game"), Mapping) else wrapper
        event = _parse_ncaa_game(raw_game, observed_at=observed_at)
        if event is not None:
            events.append(event)
    return tuple(events)


def _parse_ncaa_game(raw: Mapping[str, Any], *, observed_at: datetime) -> LiveEvent | None:
    home_payload = raw.get("home") if isinstance(raw.get("home"), Mapping) else None
    away_payload = raw.get("away") if isinstance(raw.get("away"), Mapping) else None
    if home_payload is None or away_payload is None:
        return None
    home_name = _ncaa_team_name(home_payload)
    away_name = _ncaa_team_name(away_payload)
    if home_name is None or away_name is None:
        return None
    home_id = first_text(home_payload, "teamId") or first_text(home_payload, "id")
    away_id = first_text(away_payload, "teamId") or first_text(away_payload, "id")
    game_id = first_text(raw, "gameID") or first_text(raw, "id") or ""
    game_state = (first_text(raw, "gameState") or "").lower()
    status = _map_ncaa_status(game_state)
    home_score = int_value(home_payload.get("score"))
    away_score = int_value(away_payload.get("score"))
    period_text = first_text(raw, "currentPeriod") or first_text(raw, "period")
    event_external_ids: dict[str, str] = {}
    if game_id:
        event_external_ids["ncaa"] = game_id
    home_external_ids: dict[str, str] = {}
    if home_id:
        home_external_ids["ncaa"] = home_id
    away_external_ids: dict[str, str] = {}
    if away_id:
        away_external_ids["ncaa"] = away_id
    return LiveEvent(
        source="ncaa_api",
        source_event_id=game_id,
        kind=LiveEventKind.TEAM_MATCH,
        league="NCAAB",
        sport="basketball",
        participants=(
            Participant(
                role="home",
                name=home_name,
                display_name=home_name,
                short_name=_ncaa_short_name(home_payload),
                score=home_score or 0,
                external_ids=home_external_ids,
            ),
            Participant(
                role="away",
                name=away_name,
                display_name=away_name,
                short_name=_ncaa_short_name(away_payload),
                score=away_score or 0,
                external_ids=away_external_ids,
            ),
        ),
        status=status,
        period=period_text or "",
        observed_at=observed_at,
        event_start_time=_parse_iso(first_text(raw, "startTimeEpoch") or first_text(raw, "startDate")),
        external_ids=event_external_ids,
        raw_status=game_state,
        source_payload={
            "sport": "basketball",
            "game_state": game_state,
            "period": period_text,
            "contest_clock": raw.get("contestClock"),
            "title": raw.get("title"),
            "start_time_utc": raw.get("startDate"),
        },
    )


def _ncaa_team_name(team_payload: Mapping[str, Any]) -> str | None:
    names = team_payload.get("names")
    if isinstance(names, Mapping):
        full = first_text(names, "full")
        if full:
            return full
        short = first_text(names, "short")
        if short:
            return short
    return first_text(team_payload, "name", "displayName", "shortName")


def _ncaa_short_name(team_payload: Mapping[str, Any]) -> str | None:
    names = team_payload.get("names")
    if isinstance(names, Mapping):
        return first_text(names, "short") or first_text(names, "char6") or first_text(names, "char12")
    return None


def _map_ncaa_status(state: str) -> SportsLiveGameStatus:
    if state in {"final", "ended", "completed"}:
        return SportsLiveGameStatus.ENDED
    if state in {"live", "in progress", "halftime"}:
        return SportsLiveGameStatus.LIVE if state != "halftime" else SportsLiveGameStatus.PAUSED
    if state in {"pre", "scheduled", "upcoming"}:
        return SportsLiveGameStatus.SCHEDULED
    if state in {"postponed"}:
        return SportsLiveGameStatus.POSTPONED
    if state in {"canceled", "cancelled"}:
        return SportsLiveGameStatus.CANCELLED
    return SportsLiveGameStatus.UNKNOWN


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    # epoch seconds
    try:
        parsed = float(text)
        if parsed > 1_000_000_000:
            return datetime.fromtimestamp(parsed, tz=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed_dt if parsed_dt.tzinfo is not None else parsed_dt.replace(tzinfo=timezone.utc)


__all__ = [
    "CollegeFootballDataClient",
    "parse_cfbd_games_payload",
    "parse_ncaa_api_scoreboard",
]
