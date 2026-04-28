"""NHL score API 适配器。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import httpx

from polymarket_trader.domain.sports_live import (
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
_NHL_PERIOD_SECONDS = 20 * 60


class NhlScoreApiClient:
    """读取 NHL score API 并归一化成内部比赛状态。"""

    def __init__(
        self,
        *,
        base_url: str = "https://api-web.nhle.com",
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
        """拉取 NHL 当前比赛日比分。"""

        observed_at = utc_now(self._now_provider)
        date_text = observed_at.astimezone(self._scoreboard_timezone).strftime("%Y-%m-%d")
        operation = "nhl_score"
        try:
            response = await self._client.get(f"/v1/score/{date_text}")
            response.raise_for_status()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        payload = json_mapping_from_response(response, operation=operation)
        games = parse_nhl_score_payload(payload, observed_at=observed_at)
        return SportsLiveSnapshot(source="nhl", observed_at=observed_at, games=games)


def parse_nhl_score_payload(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
) -> tuple[SportsLiveGame, ...]:
    """把 NHL score payload 转成内部比赛 DTO。"""

    observed_at = observed_at or datetime.now(timezone.utc)
    raw_games = payload.get("games")
    if not isinstance(raw_games, Sequence) or isinstance(raw_games, (str, bytes)):
        return ()
    games: list[SportsLiveGame] = []
    for raw_game in raw_games:
        if not isinstance(raw_game, Mapping):
            continue
        game = _parse_game(raw_game, observed_at=observed_at)
        if game is not None:
            games.append(game)
    return tuple(games)


def _parse_game(raw_game: Mapping[str, Any], *, observed_at: datetime) -> SportsLiveGame | None:
    home_payload = raw_game.get("homeTeam")
    away_payload = raw_game.get("awayTeam")
    if not isinstance(home_payload, Mapping) or not isinstance(away_payload, Mapping):
        return None
    home = _team_from_payload(home_payload)
    away = _team_from_payload(away_payload)
    if home is None or away is None:
        return None
    raw_status = str(raw_game.get("gameState") or "")
    clock_payload = raw_game.get("clock")
    clock = clock_payload if isinstance(clock_payload, Mapping) else {}
    period_descriptor = raw_game.get("periodDescriptor")
    period_payload = period_descriptor if isinstance(period_descriptor, Mapping) else {}
    period = int_value(raw_game.get("period")) or int_value(period_payload.get("number")) or 0
    max_regulation_periods = int_value(period_payload.get("maxRegulationPeriods")) or 3
    status = _map_status(raw_status, clock)
    return SportsLiveGame(
        source="nhl",
        source_event_id=str(raw_game.get("id") or ""),
        league="NHL",
        home=home,
        away=away,
        status=status,
        period=f"P{period}" if period > 0 else raw_status,
        seconds_remaining=_seconds_remaining(
            status=status,
            period=period,
            max_regulation_periods=max_regulation_periods,
            clock=clock,
        ),
        observed_at=observed_at,
        raw_status=raw_status,
        source_payload={
            "game_date": raw_game.get("gameDate"),
            "start_time_utc": raw_game.get("startTimeUTC"),
            "game_schedule_state": raw_game.get("gameScheduleState"),
        },
    )


def _team_from_payload(payload: Mapping[str, Any]) -> SportsLiveTeam | None:
    name_payload = payload.get("name")
    name_mapping = name_payload if isinstance(name_payload, Mapping) else {}
    name = first_text(name_mapping, "default") or first_text(payload, "name")
    if name is None:
        return None
    abbreviation = first_text(payload, "abbrev")
    return SportsLiveTeam(
        name=name,
        score=int_value(payload.get("score")) or 0,
        display_name=name,
        abbreviation=abbreviation,
        short_name=name,
        aliases=tuple(alias for alias in (name, abbreviation) if alias),
    )


def _map_status(raw_status: str, clock: Mapping[str, Any]) -> SportsLiveGameStatus:
    normalized = raw_status.strip().upper()
    if normalized in {"FUT", "PRE"}:
        return SportsLiveGameStatus.SCHEDULED
    if normalized in {"LIVE", "CRIT"}:
        if bool(clock.get("inIntermission")):
            return SportsLiveGameStatus.PAUSED
        return SportsLiveGameStatus.LIVE
    if normalized in {"OFF", "FINAL"}:
        return SportsLiveGameStatus.ENDED
    if normalized in {"POST"}:
        return SportsLiveGameStatus.POSTPONED
    if normalized in {"SUSP"}:
        return SportsLiveGameStatus.PAUSED
    return SportsLiveGameStatus.UNKNOWN


def _seconds_remaining(
    *,
    status: SportsLiveGameStatus,
    period: int,
    max_regulation_periods: int,
    clock: Mapping[str, Any],
) -> int | None:
    if status not in {SportsLiveGameStatus.LIVE, SportsLiveGameStatus.PAUSED}:
        return None
    if period <= 0:
        return None
    clock_seconds = int_value(clock.get("secondsRemaining"))
    if clock_seconds is None:
        return None
    future_periods = max(0, max_regulation_periods - period)
    return max(0, clock_seconds + future_periods * _NHL_PERIOD_SECONDS)
