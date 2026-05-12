"""NBA live scoreboard 适配器。

该模块读取 NBA 公开 liveData scoreboard，并转换成框架内部体育直播 DTO。
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
)
from polymarket_trader.infra.sports.common import (
    first_text,
    int_value,
    json_mapping_from_response,
    normalize_sports_data_error,
    utc_now,
)

_NBA_SCOREBOARD_PATH = "/static/json/liveData/scoreboard/todaysScoreboard_00.json"
_NBA_PERIOD_SECONDS = 12 * 60


class NbaLiveScoreboardClient:
    """读取 NBA live scoreboard 并归一化成内部比赛状态。"""

    def __init__(
        self,
        *,
        base_url: str = "https://cdn.nba.com",
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
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

    async def list_events(self) -> SportsLiveSnapshot:
        """拉取 NBA 当前 live scoreboard。"""

        observed_at = utc_now(self._now_provider)
        operation = "nba_live_scoreboard"
        try:
            response = await self._client.get(_NBA_SCOREBOARD_PATH)
            response.raise_for_status()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        payload = json_mapping_from_response(response, operation=operation)
        events = parse_nba_scoreboard_payload(payload, observed_at=observed_at)
        return SportsLiveSnapshot(source="nba", observed_at=observed_at, events=events)


def parse_nba_scoreboard_payload(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
) -> tuple[LiveEvent, ...]:
    """把 NBA liveData scoreboard payload 转成内部 LiveEvent DTO。"""

    observed_at = observed_at or datetime.now(timezone.utc)
    scoreboard = payload.get("scoreboard")
    scoreboard_payload = scoreboard if isinstance(scoreboard, Mapping) else payload
    raw_games = scoreboard_payload.get("games")
    if not isinstance(raw_games, Sequence) or isinstance(raw_games, (str, bytes)):
        return ()
    events: list[LiveEvent] = []
    for raw_game in raw_games:
        if not isinstance(raw_game, Mapping):
            continue
        event = _parse_game(raw_game, observed_at=observed_at)
        if event is not None:
            events.append(event)
    return tuple(events)


def _parse_game(raw_game: Mapping[str, Any], *, observed_at: datetime) -> LiveEvent | None:
    home_payload = raw_game.get("homeTeam")
    away_payload = raw_game.get("awayTeam")
    if not isinstance(home_payload, Mapping) or not isinstance(away_payload, Mapping):
        return None
    home = _team_from_payload(home_payload, role="home")
    away = _team_from_payload(away_payload, role="away")
    if home is None or away is None:
        return None
    status = _map_status(raw_game)
    regulation_periods = int_value(raw_game.get("regulationPeriods")) or 4
    period_number = int_value(raw_game.get("period")) or 0
    source_event_id = str(raw_game.get("gameId") or raw_game.get("gameCode") or "")
    return LiveEvent(
        source="nba",
        source_event_id=source_event_id,
        kind=LiveEventKind.TEAM_MATCH,
        league="NBA",
        sport="basketball",
        participants=(home, away),
        status=status,
        period=_period_label(period_number, raw_game),
        seconds_remaining=_seconds_remaining(
            status=status,
            period=period_number,
            regulation_periods=regulation_periods,
            clock=raw_game.get("gameClock"),
        ),
        observed_at=observed_at,
        raw_status=first_text(raw_game, "gameStatusText") or str(raw_game.get("gameStatus") or ""),
        external_ids={"nba": source_event_id} if source_event_id else {},
        source_payload={
            "game_code": raw_game.get("gameCode"),
            "game_time_utc": raw_game.get("gameTimeUTC"),
            "game_et": raw_game.get("gameEt"),
        },
    )


def _team_from_payload(payload: Mapping[str, Any], *, role: str) -> Participant | None:
    team_name = first_text(payload, "teamName")
    if team_name is None:
        return None
    city = first_text(payload, "teamCity")
    abbreviation = first_text(payload, "teamTricode")
    display_name = f"{city} {team_name}".strip() if city else team_name
    team_id = first_text(payload, "teamId")
    return Participant(
        role=role,
        name=team_name,
        score=int_value(payload.get("score")) or 0,
        display_name=display_name,
        abbreviation=abbreviation,
        short_name=team_name,
        location=city,
        aliases=tuple(alias for alias in (display_name, city, abbreviation) if alias),
        external_ids={"nba": team_id} if team_id else {},
    )


def _map_status(raw_game: Mapping[str, Any]) -> SportsLiveGameStatus:
    status_code = int_value(raw_game.get("gameStatus"))
    status_text = str(raw_game.get("gameStatusText") or "").lower()
    if status_code == 1:
        return SportsLiveGameStatus.SCHEDULED
    if status_code == 2:
        if "halftime" in status_text or "intermission" in status_text:
            return SportsLiveGameStatus.PAUSED
        return SportsLiveGameStatus.LIVE
    if status_code == 3:
        return SportsLiveGameStatus.ENDED
    if "postponed" in status_text:
        return SportsLiveGameStatus.POSTPONED
    if "cancel" in status_text:
        return SportsLiveGameStatus.CANCELLED
    return SportsLiveGameStatus.UNKNOWN


def _seconds_remaining(
    *,
    status: SportsLiveGameStatus,
    period: int,
    regulation_periods: int,
    clock: Any,
) -> int | None:
    if status not in {SportsLiveGameStatus.LIVE, SportsLiveGameStatus.PAUSED}:
        return None
    if period <= 0:
        return None
    clock_seconds = _iso_clock_seconds(clock)
    if clock_seconds is None:
        return None
    future_periods = max(0, regulation_periods - period)
    return max(0, clock_seconds + future_periods * _NBA_PERIOD_SECONDS)


def _iso_clock_seconds(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"PT(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", text)
    if match:
        minutes = int(match.group(1) or 0)
        seconds = int(float(match.group(2) or 0))
        return minutes * 60 + seconds
    parts = text.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(float(parts[1]))
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _period_label(period: int, raw_game: Mapping[str, Any]) -> str:
    if period > 0:
        return f"Q{period}"
    return first_text(raw_game, "gameStatusText") or ""
