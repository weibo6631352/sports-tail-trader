"""MLB Stats API schedule 适配器。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import httpx

from polymarket_trader.domain.sports_live import (
    BaseballGameState,
    SportsLiveGame,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
    SportsLiveTeam,
)
from polymarket_trader.infra.sports.common import (
    first_text,
    int_value,
    json_mapping_from_response,
    normalize_sports_data_error,
    timezone_for,
    utc_now,
)

_DEFAULT_SCOREBOARD_TIMEZONE = "America/New_York"


class MlbStatsApiClient:
    """读取 MLB Stats API schedule 并归一化成内部比赛状态。"""

    def __init__(
        self,
        *,
        base_url: str = "https://statsapi.mlb.com",
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
        scoreboard_timezone: str = _DEFAULT_SCOREBOARD_TIMEZONE,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._scoreboard_timezone = timezone_for(scoreboard_timezone)
        self._now_provider = now_provider
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_s,
            headers={"accept": "application/json"},
            trust_env=False,
        )

    async def aclose(self) -> None:
        """关闭内部 HTTP client。"""

        if self._owns_client:
            await self._client.aclose()

    async def list_games(self) -> SportsLiveSnapshot:
        """拉取 MLB 当前比赛日 schedule。"""

        observed_at = utc_now(self._now_provider)
        date_text = observed_at.astimezone(self._scoreboard_timezone).strftime("%m/%d/%Y")
        operation = "mlb_schedule"
        try:
            response = await self._client.get(
                "/api/v1/schedule",
                params={"sportId": 1, "hydrate": "linescore,team", "date": date_text},
            )
            response.raise_for_status()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        payload = json_mapping_from_response(response, operation=operation)
        games = parse_mlb_schedule_payload(payload, observed_at=observed_at)
        return SportsLiveSnapshot(source="mlb", observed_at=observed_at, games=games)


def parse_mlb_schedule_payload(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
) -> tuple[SportsLiveGame, ...]:
    """把 MLB schedule payload 转成内部比赛 DTO。"""

    observed_at = observed_at or datetime.now(timezone.utc)
    dates = payload.get("dates")
    if not isinstance(dates, Sequence) or isinstance(dates, (str, bytes)):
        return ()
    games: list[SportsLiveGame] = []
    for date_payload in dates:
        if not isinstance(date_payload, Mapping):
            continue
        raw_games = date_payload.get("games")
        if not isinstance(raw_games, Sequence) or isinstance(raw_games, (str, bytes)):
            continue
        for raw_game in raw_games:
            if not isinstance(raw_game, Mapping):
                continue
            game = _parse_game(raw_game, observed_at=observed_at)
            if game is not None:
                games.append(game)
    return tuple(games)


def _parse_game(raw_game: Mapping[str, Any], *, observed_at: datetime) -> SportsLiveGame | None:
    teams = raw_game.get("teams")
    teams_payload = teams if isinstance(teams, Mapping) else {}
    home_payload = teams_payload.get("home")
    away_payload = teams_payload.get("away")
    if not isinstance(home_payload, Mapping) or not isinstance(away_payload, Mapping):
        return None
    home = _team_from_payload(home_payload)
    away = _team_from_payload(away_payload)
    if home is None or away is None:
        return None
    status_payload = raw_game.get("status")
    status_mapping = status_payload if isinstance(status_payload, Mapping) else {}
    raw_status = first_text(status_mapping, "detailedState", "abstractGameState", "statusCode")
    status = _map_status(status_mapping)
    linescore = raw_game.get("linescore")
    linescore_payload = linescore if isinstance(linescore, Mapping) else {}
    return SportsLiveGame(
        source="mlb",
        source_event_id=str(raw_game.get("gamePk") or raw_game.get("gameGuid") or ""),
        league="MLB",
        home=home,
        away=away,
        status=status,
        period=_period_label(linescore_payload, raw_status),
        seconds_remaining=None,
        observed_at=observed_at,
        raw_status=raw_status,
        baseball_state=_baseball_state(linescore_payload),
        source_payload={
            "game_date": raw_game.get("gameDate"),
            "official_date": raw_game.get("officialDate"),
            "link": raw_game.get("link"),
        },
    )


def _team_from_payload(payload: Mapping[str, Any]) -> SportsLiveTeam | None:
    team = payload.get("team")
    team_payload = team if isinstance(team, Mapping) else {}
    team_name = first_text(team_payload, "teamName", "clubName", "name")
    if team_name is None:
        return None
    full_name = first_text(team_payload, "name") or team_name
    location = first_text(team_payload, "locationName", "franchiseName")
    abbreviation = first_text(team_payload, "abbreviation", "fileCode", "teamCode")
    return SportsLiveTeam(
        name=team_name,
        score=int_value(payload.get("score")) or 0,
        display_name=full_name,
        abbreviation=abbreviation,
        short_name=team_name,
        location=location,
        aliases=tuple(
            alias
            for alias in (
                full_name,
                location,
                first_text(team_payload, "shortName"),
                first_text(team_payload, "franchiseName"),
                first_text(team_payload, "clubName"),
                abbreviation,
            )
            if alias
        ),
    )


def _map_status(status: Mapping[str, Any]) -> SportsLiveGameStatus:
    abstract = str(status.get("abstractGameState") or "").strip().lower()
    detailed = str(status.get("detailedState") or "").strip().lower()
    code = str(status.get("statusCode") or "").strip().upper()
    if "postponed" in detailed:
        return SportsLiveGameStatus.POSTPONED
    if "cancel" in detailed:
        return SportsLiveGameStatus.CANCELLED
    if "delay" in detailed or "suspend" in detailed:
        return SportsLiveGameStatus.PAUSED
    if abstract == "live" or code in {"I", "M", "N"}:
        return SportsLiveGameStatus.LIVE
    if abstract == "preview" or code in {"S", "P"}:
        return SportsLiveGameStatus.SCHEDULED
    if abstract == "final" or code in {"F", "O"}:
        return SportsLiveGameStatus.ENDED
    return SportsLiveGameStatus.UNKNOWN


def _period_label(linescore: Mapping[str, Any], raw_status: str | None) -> str:
    inning = int_value(linescore.get("currentInning"))
    if inning is None or inning <= 0:
        return raw_status or ""
    half = str(linescore.get("inningHalf") or "").strip().lower()
    if half.startswith("top"):
        return f"T{inning}"
    if half.startswith("bottom"):
        return f"B{inning}"
    if half.startswith("middle"):
        return f"M{inning}"
    if half.startswith("end"):
        return f"E{inning}"
    return f"I{inning}"


def _baseball_state(linescore: Mapping[str, Any]) -> BaseballGameState | None:
    inning = int_value(linescore.get("currentInning"))
    inning_half = first_text(linescore, "inningHalf")
    outs = int_value(linescore.get("outs"))
    offense = linescore.get("offense")
    defense = linescore.get("defense")
    offense_mapping = offense if isinstance(offense, Mapping) else {}
    defense_mapping = defense if isinstance(defense, Mapping) else {}
    occupied_bases = tuple(
        base_number
        for base_number, key in ((1, "first"), (2, "second"), (3, "third"))
        if isinstance(offense_mapping.get(key), Mapping)
    )
    if inning is None and inning_half is None and outs is None and not offense_mapping and not defense_mapping:
        return None
    return BaseballGameState(
        current_inning=inning,
        inning_half=None if inning_half is None else inning_half.strip().lower(),
        outs=outs,
        offense_team=_team_name_from_linescore_side(offense_mapping),
        defense_team=_team_name_from_linescore_side(defense_mapping),
        occupied_bases=occupied_bases,
    )


def _team_name_from_linescore_side(payload: Mapping[str, Any]) -> str | None:
    team = payload.get("team")
    team_mapping = team if isinstance(team, Mapping) else {}
    return first_text(team_mapping, "name", "teamName", "clubName")
