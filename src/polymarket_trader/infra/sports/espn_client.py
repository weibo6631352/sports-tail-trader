"""ESPN scoreboard REST 适配器。

该模块只负责协议访问和字段归一化，不判断盘口、仓位或是否交易。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from polymarket_trader.domain.sports_live import (
    CricketGameState,
    SportsLiveDriverPosition,
    SportsLiveGame,
    SportsLiveGameStatus,
    SportsLiveRaceEvent,
    SportsLiveSnapshot,
    SportsLiveTeam,
)
from polymarket_trader.infra.sports.common import (
    SportsDataClientError,
    SportsDataResponseError,
    json_mapping_from_response,
    normalize_sports_data_error,
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
    "atp": "/apis/site/v2/sports/tennis/atp/scoreboard",
    "wta": "/apis/site/v2/sports/tennis/wta/scoreboard",
    # MMA / 拳击：ESPN scoreboard 把 fighter 当 home/away 二人对位，沿用 team-pair 解析。
    "ufc": "/apis/site/v2/sports/mma/ufc/scoreboard",
    "mma": "/apis/site/v2/sports/mma/ufc/scoreboard",
    # 橄榄球：英式/澳式都按 team-pair 输出，复用现有解析。
    "rugby": "/apis/site/v2/sports/rugby/scoreboard",
    # 板球：team-pair 形态，但需要 CricketGameState 解析（innings / runs / wickets / overs）。
    "intl-test": "/apis/site/v2/sports/cricket/intl-test/scoreboard",
    "intl-t20i": "/apis/site/v2/sports/cricket/intl-t20i/scoreboard",
    "intl-odi": "/apis/site/v2/sports/cricket/intl-odi/scoreboard",
    "ipl": "/apis/site/v2/sports/cricket/ipl/scoreboard",
    "bbl": "/apis/site/v2/sports/cricket/bbl/scoreboard",
    # 赛车：field event 形态，不能映射 team-pair；走 SportsLiveRaceEvent 单独通路。
    "f1": "/apis/site/v2/sports/racing/f1/scoreboard",
    "nascar": "/apis/site/v2/sports/racing/nascar-premier/scoreboard",
    "indycar": "/apis/site/v2/sports/racing/irl/scoreboard",
}

_CRICKET_LEAGUES: frozenset[str] = frozenset(
    {"intl-test", "intl-t20i", "intl-odi", "ipl", "bbl", "cricket"}
)

_RACE_LEAGUES: frozenset[str] = frozenset({"f1", "nascar", "indycar"})

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
        """拉取所有配置联赛的当前 scoreboard。

        team-pair 类联赛产出 ``SportsLiveGame``；赛车类联赛产出
        ``SportsLiveRaceEvent``（不能映射 home/away）。两类在同一 snapshot 里
        分别放在 ``games`` 与 ``race_events`` 字段。
        """

        observed_at = self._now()
        scoreboard_dates = _scoreboard_dates(
            observed_at,
            timezone_=self._scoreboard_timezone,
            days_before=self._date_window_days_before,
            days_after=self._date_window_days_after,
        )
        games: list[SportsLiveGame] = []
        race_events: list[SportsLiveRaceEvent] = []
        seen_event_keys: set[tuple[str, str]] = set()
        seen_race_keys: set[tuple[str, str]] = set()
        failures: list[SportsDataClientError] = []
        successful_requests = 0
        for league in self._leagues:
            for scoreboard_date in scoreboard_dates:
                try:
                    payload = await self._get_scoreboard(league, scoreboard_date=scoreboard_date)
                except SportsDataClientError as exc:
                    failures.append(exc)
                    continue
                successful_requests += 1
                if league in _RACE_LEAGUES:
                    for race in parse_espn_race_payload(
                        payload,
                        league=league,
                        observed_at=observed_at,
                    ):
                        race_key = (race.league, race.source_event_id)
                        if race.source_event_id and race_key in seen_race_keys:
                            continue
                        seen_race_keys.add(race_key)
                        race_events.append(race)
                    continue
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
        if successful_requests <= 0 and failures:
            raise failures[0]
        return SportsLiveSnapshot(
            source="espn",
            observed_at=observed_at,
            games=tuple(games),
            race_events=tuple(race_events),
        )

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
        return json_mapping_from_response(response, operation=operation)

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

    cricket_state = (
        _cricket_state_from_competition(competition, competitors)
        if league in _CRICKET_LEAGUES
        else None
    )

    return SportsLiveGame(
        source="espn",
        source_event_id=str(event.get("id") or competition.get("id") or ""),
        league=league.upper(),
        home=home,
        away=away,
        cricket_state=cricket_state,
        status=status,
        period=period,
        seconds_remaining=seconds_remaining,
        observed_at=observed_at,
        raw_status=raw_status,
        source_payload={
            "event_id": event.get("id"),
            "name": event.get("name"),
            "short_name": event.get("shortName"),
            "start_time_utc": event.get("date") or competition.get("date"),
        },
    )


def parse_espn_race_payload(
    payload: Mapping[str, Any],
    *,
    league: str,
    observed_at: datetime | None = None,
) -> tuple[SportsLiveRaceEvent, ...]:
    """把 ESPN 赛车 scoreboard 转成 ``SportsLiveRaceEvent`` 序列。

    赛车类 scoreboard 与 team-pair 类不同：
    - 每个 event 含 ``competitions[0].competitors`` 数组，但每个 competitor 是
      "车手 + 车队"（athlete + team），并且没有 home/away 标记；
    - linescore / score 字段是位次而非比分；
    - 圈数 / 总圈数在 ``competitions[0].status.{period,detail}`` 或
      ``competitions[0].laps``。
    """

    observed_at = observed_at or datetime.now(timezone.utc)
    league = _normalize_league_code(league)
    events = payload.get("events")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        return ()
    races: list[SportsLiveRaceEvent] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        race = _parse_race_event(event, league=league, observed_at=observed_at)
        if race is not None:
            races.append(race)
    return tuple(races)


def _parse_race_event(
    event: Mapping[str, Any],
    *,
    league: str,
    observed_at: datetime,
) -> SportsLiveRaceEvent | None:
    competitions = event.get("competitions")
    if not isinstance(competitions, Sequence) or not competitions:
        return None
    competition = competitions[0]
    if not isinstance(competition, Mapping):
        return None
    status_payload = event.get("status")
    if not isinstance(status_payload, Mapping):
        status_payload = competition.get("status")
    if not isinstance(status_payload, Mapping):
        status_payload = {}
    status = _map_status(status_payload)
    raw_status = _status_name(status_payload)
    laps_completed = _int_value(competition.get("lapsCompleted") or competition.get("currentLap"))
    total_laps = _int_value(competition.get("totalLaps") or competition.get("scheduledLaps"))
    competitors = competition.get("competitors")
    competitors_seq = competitors if isinstance(competitors, Sequence) else ()
    drivers: list[SportsLiveDriverPosition] = []
    leader_driver: str | None = None
    leader_team: str | None = None
    for competitor in competitors_seq:
        position = _driver_position_from(competitor)
        if position is None:
            continue
        drivers.append(position)
        if leader_driver is None and position.position == 1:
            leader_driver = position.driver
            leader_team = position.team
    if leader_driver is None and drivers:
        # 按 position 排序后取第一个非空 position 作为 leader 兜底
        ordered = sorted(drivers, key=lambda d: d.position or 10**6)
        leader_driver = ordered[0].driver
        leader_team = ordered[0].team
    event_name = _first_text(event, "name", "shortName") or league.upper()
    return SportsLiveRaceEvent(
        source="espn",
        source_event_id=str(event.get("id") or competition.get("id") or ""),
        league=league.upper(),
        event_name=event_name,
        status=status,
        leader_driver=leader_driver,
        leader_team=leader_team,
        laps_completed=laps_completed,
        total_laps=total_laps,
        status_flag=_first_text(status_payload, "flagState", "detail"),
        observed_at=observed_at,
        raw_status=raw_status,
        drivers=tuple(drivers),
        source_payload={
            "start_time_utc": event.get("date") or competition.get("date"),
            "venue": _first_text(competition.get("venue") or {}, "fullName", "displayName") if isinstance(competition.get("venue"), Mapping) else None,
        },
    )


def _driver_position_from(competitor: Any) -> SportsLiveDriverPosition | None:
    if not isinstance(competitor, Mapping):
        return None
    athlete = competitor.get("athlete")
    athlete_mapping = athlete if isinstance(athlete, Mapping) else {}
    driver_name = _first_text(athlete_mapping, "displayName", "shortName", "fullName")
    if driver_name is None:
        driver_name = _first_text(competitor, "displayName", "name")
    if driver_name is None:
        return None
    team_payload = competitor.get("team")
    team_mapping = team_payload if isinstance(team_payload, Mapping) else {}
    team_name = _first_text(team_mapping, "displayName", "name", "shortDisplayName")
    position = _int_value(competitor.get("status", {}).get("position") if isinstance(competitor.get("status"), Mapping) else None)
    if position is None:
        position = _int_value(competitor.get("position"))
    laps_completed = _int_value(
        competitor.get("lapsCompleted")
        or (competitor.get("status", {}).get("laps") if isinstance(competitor.get("status"), Mapping) else None)
    )
    gap_to_leader = _first_text(competitor, "behindBy", "gap")
    status_label = (
        _first_text(competitor.get("status") or {}, "displayName", "shortName")
        if isinstance(competitor.get("status"), Mapping)
        else None
    )
    return SportsLiveDriverPosition(
        driver=driver_name,
        position=position,
        team=team_name,
        laps_completed=laps_completed,
        gap_to_leader=gap_to_leader,
        status=status_label,
    )


def _cricket_state_from_competition(
    competition: Mapping[str, Any],
    competitors: Sequence[Any],
) -> CricketGameState | None:
    """从 ESPN 板球 scoreboard 提取局/分/wickets/overs。

    ESPN 板球 ``situation`` 字段不稳定，但 ``competitors[i].statistics`` 在 live
    比赛中通常含 wickets / overs 数据；``competitors[i].score`` 是总分。
    这里只填能稳定提取的字段，缺数据时返回 None 表示该 game 没有结构化板球态。
    """

    batting_side: str | None = None
    runs: int | None = None
    wickets: int | None = None
    overs_completed: int | None = None
    balls_in_over: int | None = None
    target: int | None = None
    required_runs: int | None = None
    required_balls: int | None = None
    situation = competition.get("situation")
    situation_mapping = situation if isinstance(situation, Mapping) else {}
    batting = situation_mapping.get("batting")
    batting_mapping = batting if isinstance(batting, Mapping) else {}
    batting_name = _first_text(batting_mapping, "displayName", "name", "shortName")
    for competitor in competitors:
        if not isinstance(competitor, Mapping):
            continue
        team_payload = competitor.get("team")
        team_mapping = team_payload if isinstance(team_payload, Mapping) else {}
        team_name = _first_text(team_mapping, "displayName", "name", "shortDisplayName")
        if batting_name and team_name and batting_name == team_name:
            batting_side = (str(competitor.get("homeAway") or "").lower() or None)
            runs = _int_value(competitor.get("score"))
            stats = competitor.get("statistics") if isinstance(competitor.get("statistics"), Sequence) else ()
            for stat in stats:
                if not isinstance(stat, Mapping):
                    continue
                name = str(stat.get("name") or "").lower()
                value = _int_value(stat.get("value")) if stat.get("value") is not None else None
                if name in {"wickets", "wicketstotal"} and value is not None:
                    wickets = value
                elif name in {"oversbowled", "overs"}:
                    raw = stat.get("displayValue") or stat.get("value")
                    overs_completed, balls_in_over = _cricket_overs_parts(raw)
    target = _int_value(situation_mapping.get("targetRuns") or situation_mapping.get("target"))
    required_runs = _int_value(situation_mapping.get("requiredRuns"))
    required_balls = _int_value(situation_mapping.get("requiredBalls"))
    if all(
        value is None
        for value in (batting_side, runs, wickets, overs_completed, target)
    ):
        return None
    current_innings = _int_value(situation_mapping.get("currentInnings") or situation_mapping.get("inning"))
    return CricketGameState(
        current_innings=current_innings,
        batting_side=batting_side,
        runs=runs,
        wickets=wickets,
        overs_completed=overs_completed,
        balls_in_over=balls_in_over,
        target=target,
        required_runs=required_runs,
        required_balls=required_balls,
    )


def _cricket_overs_parts(raw: Any) -> tuple[int | None, int | None]:
    """把 "12.3" / "12" / 12.3 拆成 ``(overs:int, balls_in_over:int)``。

    板球 overs 是非十进制：``12.3`` 表示 12 完整 overs + 3 球（每 over 6 球），
    所以不能简单按浮点解释。
    """

    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text:
        return None, None
    if "." in text:
        parts = text.split(".", 1)
        try:
            overs = int(float(parts[0]))
        except ValueError:
            return None, None
        try:
            balls = int(float(parts[1]))
        except ValueError:
            return overs, None
        if 0 <= balls <= 5:
            return overs, balls
        return overs, None
    try:
        return int(float(text)), 0
    except ValueError:
        return None, None


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
    return normalize_sports_data_error(exc, operation=operation)


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
