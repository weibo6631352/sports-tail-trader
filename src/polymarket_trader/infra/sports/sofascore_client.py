"""SofaScore 公开比分源适配器。

该模块读取 SofaScore scheduled-events JSON，把多运动项目的比赛状态归一化成
内部体育直播 DTO。SofaScore 作为免费通用补充源，默认只按配置联赛映射到必要
sport path，避免每轮同步拉取无关的大体量赛程。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import re
from typing import Any

import httpx

from polymarket_trader.domain.sports_live import (
    BaseballGameState,
    SoccerGameState,
    SportsLiveGame,
    SportsLiveGameStatus,
    SportsLiveSnapshot,
    SportsLiveSourceHealth,
    SportsLiveSourceStatus,
    SportsLiveTeam,
    TennisGameState,
)
from polymarket_trader.infra.sports.common import (
    SportsDataRateLimitError,
    first_text,
    int_value,
    json_mapping_from_response,
    normalize_sports_data_error,
    utc_now,
)

_DEFAULT_BASE_URL = "https://www.sofascore.com"
_WHOLE_MARKET_SPORTS = (
    "basketball",
    "ice-hockey",
    "baseball",
    "american-football",
    "football",
    "tennis",
    "table-tennis",
)
_DEFAULT_SPORTS = ("basketball", "ice-hockey", "baseball", "american-football")
_SCHEDULED_EVENTS_PATH = "/api/v1/sport/{sport}/scheduled-events/{date}"

_SPORTS_BY_LEAGUE: dict[str, tuple[str, ...]] = {
    "sports": _WHOLE_MARKET_SPORTS,
    "nba": ("basketball",),
    "wnba": ("basketball",),
    "ncaamb": ("basketball",),
    "ncaawb": ("basketball",),
    "basketball": ("basketball",),
    "nhl": ("ice-hockey",),
    "ice-hockey": ("ice-hockey",),
    "hockey": ("ice-hockey",),
    "mlb": ("baseball",),
    "baseball": ("baseball",),
    "nfl": ("american-football",),
    "ncaaf": ("american-football",),
    "american-football": ("american-football",),
    "soccer": ("football",),
    "epl": ("football",),
    "premier-league": ("football",),
    "football": ("football",),
    "tennis": ("tennis",),
    "wtt": ("table-tennis",),
    "table-tennis": ("table-tennis",),
    "table tennis": ("table-tennis",),
}

_TOURNAMENT_ALIASES_BY_LEAGUE: dict[str, tuple[str, ...] | None] = {
    "sports": None,
    "nba": ("nba", "national basketball association"),
    "wnba": ("wnba", "women national basketball association"),
    "ncaamb": ("ncaa", "ncaa men", "college basketball"),
    "ncaawb": ("ncaa", "ncaa women", "college basketball"),
    "basketball": None,
    "nhl": ("nhl", "national hockey league"),
    "ice-hockey": None,
    "hockey": None,
    "mlb": ("mlb", "major league baseball"),
    "baseball": None,
    "nfl": ("nfl", "national football league"),
    "ncaaf": ("ncaa", "college football"),
    "american-football": None,
    "soccer": None,
    "football": None,
    "epl": ("premier league", "premier-league"),
    "premier-league": ("premier league", "premier-league"),
    "tennis": None,
    "wtt": ("world team championships", "wtt"),
    "table-tennis": None,
    "table tennis": None,
}


class SofaScoreLiveClient:
    """读取 SofaScore scheduled-events 并归一化成内部比赛状态。"""

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        sports: Sequence[str] | None = None,
        league_codes: Sequence[str] = ("nba", "nhl", "nfl", "mlb"),
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
        min_fetch_interval_s: float = 20.0,
        max_stale_on_error_s: float = 300.0,
        lookback_days: int = 0,
        lookahead_days: int = 0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._league_codes = _normalize_codes(league_codes)
        self._sports = _normalize_codes(sports or sofascore_sports_for_leagues(self._league_codes) or _DEFAULT_SPORTS)
        self._now_provider = now_provider
        self._min_fetch_interval_s = max(0.0, float(min_fetch_interval_s))
        self._max_stale_on_error_s = max(0.0, float(max_stale_on_error_s))
        self._lookback_days = max(0, int(lookback_days))
        self._lookahead_days = max(0, int(lookahead_days))
        self._cached_snapshot: SportsLiveSnapshot | None = None
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_s,
            headers={
                "accept": "application/json",
                "user-agent": "sports-tail-trader/0.1 (+https://polymarket.com)",
            },
            trust_env=False,
        )

    @property
    def sports(self) -> tuple[str, ...]:
        """返回当前 client 会拉取的 SofaScore sport path。"""

        return self._sports

    async def aclose(self) -> None:
        """关闭内部 HTTP client。"""

        if self._owns_client:
            await self._client.aclose()

    async def list_games(self) -> SportsLiveSnapshot:
        """拉取配置 sport 的当前 UTC 比赛日数据。"""

        observed_at = utc_now(self._now_provider)
        if self._is_cache_fresh(observed_at):
            return self._snapshot_with_status(
                self._cached_snapshot or SportsLiveSnapshot(source="sofascore", observed_at=observed_at, games=()),
                health=SportsLiveSourceHealth.CACHED,
            )
        date_texts = _scheduled_event_dates(
            observed_at,
            lookback_days=self._lookback_days,
            lookahead_days=self._lookahead_days,
        )
        try:
            requests = tuple((sport, date_text) for sport in self._sports for date_text in date_texts)
            results = await asyncio.gather(
                *(self._get_scheduled_events(sport, date_text=date_text) for sport, date_text in requests),
                return_exceptions=True,
            )
            failures = [
                f"{sport}:{date_text}: {result}"
                for (sport, date_text), result in zip(requests, results, strict=True)
                if isinstance(result, Exception)
            ]
            payloads = [
                (sport, result)
                for (sport, _date_text), result in zip(requests, results, strict=True)
                if not isinstance(result, Exception)
            ]
            if not payloads and failures:
                first_error = next(result for result in results if isinstance(result, Exception))
                raise first_error
            games = [
                game
                for sport, payload in payloads
                for game in parse_sofascore_events_payload(
                    payload,
                    sport=sport,
                    league_codes=self._league_codes,
                    observed_at=observed_at,
                )
            ]
        except Exception as exc:
            if self._is_cache_usable_after_error(observed_at):
                return self._snapshot_with_status(
                    self._cached_snapshot or SportsLiveSnapshot(source="sofascore", observed_at=observed_at, games=()),
                    health=SportsLiveSourceHealth.CACHED,
                )
            if isinstance(exc, SportsDataRateLimitError):
                snapshot = self._snapshot_with_status(
                    SportsLiveSnapshot(source="sofascore", observed_at=observed_at, games=()),
                    health=SportsLiveSourceHealth.RATE_LIMITED,
                    success=False,
                    last_error=str(exc),
                )
                self._cached_snapshot = snapshot
                return snapshot
            raise
        snapshot = SportsLiveSnapshot(source="sofascore", observed_at=observed_at, games=tuple(games))
        snapshot = self._snapshot_with_status(
            snapshot,
            health=(
                SportsLiveSourceHealth.SUCCESS_WITH_LIVE_DATA
                if snapshot.games
                else SportsLiveSourceHealth.SUCCESS_EMPTY
            ),
            last_error="; ".join(failures) if failures else None,
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
            source="sofascore",
            observed_at=snapshot.observed_at,
            games=snapshot.games,
            source_statuses=(
                SportsLiveSourceStatus(
                    source="sofascore",
                    success=success,
                    health=health,
                    games_seen=len(snapshot.games),
                    observed_at=snapshot.observed_at,
                    last_error=last_error,
                ),
            ),
        )

    async def _get_scheduled_events(self, sport: str, *, date_text: str) -> Mapping[str, Any]:
        operation = f"sofascore_scheduled_events:{sport}"
        try:
            response = await self._client.get(_SCHEDULED_EVENTS_PATH.format(sport=sport, date=date_text))
            response.raise_for_status()
        except Exception as exc:
            raise normalize_sports_data_error(exc, operation=operation) from exc
        return json_mapping_from_response(response, operation=operation)


def sofascore_sports_for_leagues(league_codes: Sequence[str]) -> tuple[str, ...]:
    """按内部联赛代码返回 SofaScore sport path，保持顺序并去重。"""

    seen: set[str] = set()
    result: list[str] = []
    for code in _normalize_codes(league_codes):
        for sport in _SPORTS_BY_LEAGUE.get(code, ()):
            if sport in seen:
                continue
            seen.add(sport)
            result.append(sport)
    return tuple(result)


def _scheduled_event_dates(observed_at: datetime, *, lookback_days: int, lookahead_days: int) -> tuple[str, ...]:
    """返回需要拉取的 UTC 比赛日，覆盖近期未结算和近未来单场市场。"""

    start = observed_at.date()
    return tuple(
        (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(-max(0, lookback_days), max(0, lookahead_days) + 1)
    )


def parse_sofascore_events_payload(
    payload: Mapping[str, Any],
    *,
    sport: str,
    league_codes: Sequence[str] = (),
    observed_at: datetime | None = None,
) -> tuple[SportsLiveGame, ...]:
    """把 SofaScore scheduled-events payload 转成内部比赛 DTO。"""

    observed_at = observed_at or datetime.now(timezone.utc)
    raw_events = payload.get("events")
    if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
        return ()
    games: list[SportsLiveGame] = []
    normalized_sport = str(sport).strip().lower()
    allowed_aliases = _allowed_tournament_aliases(league_codes)
    for raw_event in raw_events:
        if not isinstance(raw_event, Mapping):
            continue
        if not _event_matches_leagues(raw_event, allowed_aliases):
            continue
        game = _parse_event(raw_event, sport=normalized_sport, observed_at=observed_at)
        if game is not None:
            games.append(game)
    return tuple(games)


def _parse_event(raw_event: Mapping[str, Any], *, sport: str, observed_at: datetime) -> SportsLiveGame | None:
    home_payload = raw_event.get("homeTeam")
    away_payload = raw_event.get("awayTeam")
    if not isinstance(home_payload, Mapping) or not isinstance(away_payload, Mapping):
        return None
    home = _team_from_payload(home_payload, raw_event.get("homeScore"))
    away = _team_from_payload(away_payload, raw_event.get("awayScore"))
    if home is None or away is None:
        return None
    status_payload = raw_event.get("status")
    status_mapping = status_payload if isinstance(status_payload, Mapping) else {}
    status = _map_status(status_mapping)
    raw_status = first_text(status_mapping, "description", "type") or ""
    tournament = _tournament_payload(raw_event)
    tennis_state = _tennis_state_from_payload(raw_event, raw_status=raw_status) if sport == "tennis" else None
    baseball_state = _baseball_state_from_payload(raw_event, raw_status=raw_status) if sport == "baseball" else None
    soccer_state = _soccer_state_from_payload(raw_event, raw_status=raw_status) if sport == "football" else None
    return SportsLiveGame(
        source="sofascore",
        source_event_id=str(raw_event.get("id") or raw_event.get("customId") or ""),
        league=_league_name(tournament, sport=sport),
        home=home,
        away=away,
        status=status,
        period=_period_label(sport=sport, raw_status=raw_status),
        seconds_remaining=_seconds_remaining(
            sport=sport,
            status=status,
            time_payload=raw_event.get("time"),
        ),
        observed_at=observed_at,
        raw_status=raw_status,
        baseball_state=baseball_state,
        tennis_state=tennis_state,
        soccer_state=soccer_state,
        source_payload={
            "sport": sport,
            "slug": raw_event.get("slug"),
            "start_timestamp": raw_event.get("startTimestamp"),
            "tournament": tournament.get("name"),
            "tournament_slug": tournament.get("slug"),
        },
    )


def _team_from_payload(payload: Mapping[str, Any], score_payload: Any) -> SportsLiveTeam | None:
    name = first_text(payload, "name", "shortName", "nameCode")
    if name is None:
        return None
    score_mapping = score_payload if isinstance(score_payload, Mapping) else {}
    short_name = first_text(payload, "shortName")
    abbreviation = first_text(payload, "nameCode")
    slug = first_text(payload, "slug")
    return SportsLiveTeam(
        name=name,
        score=_score_value(score_mapping),
        display_name=name,
        abbreviation=abbreviation,
        short_name=short_name,
        location=None,
        aliases=tuple(alias for alias in (short_name, abbreviation, _slug_alias(slug)) if alias),
    )


def _score_value(payload: Mapping[str, Any]) -> int:
    value = int_value(payload.get("current"))
    if value is None:
        value = int_value(payload.get("display"))
    return value or 0


def _tennis_state_from_payload(raw_event: Mapping[str, Any], *, raw_status: str) -> TennisGameState | None:
    """提取网球当前盘分和总局数，供策略层做尾盘判断。

    SofaScore 的 ``current`` 表示已赢盘数，``periodN`` 表示每盘局数，
    ``point`` 表示当前局即时分。这里不推断赛制，只提供结构化事实。
    """

    home_score = raw_event.get("homeScore")
    away_score = raw_event.get("awayScore")
    home_mapping = home_score if isinstance(home_score, Mapping) else {}
    away_mapping = away_score if isinstance(away_score, Mapping) else {}
    current_set = _tennis_current_set(raw_status, home_mapping, away_mapping)
    home_current_games = _period_score(home_mapping, current_set)
    away_current_games = _period_score(away_mapping, current_set)
    home_total_games = _period_total(home_mapping)
    away_total_games = _period_total(away_mapping)
    first_to_serve = _tennis_serving_side(raw_event.get("firstToServe"))
    return TennisGameState(
        home_sets_won=_score_value(home_mapping),
        away_sets_won=_score_value(away_mapping),
        current_set=current_set,
        home_current_set_games=home_current_games,
        away_current_set_games=away_current_games,
        home_total_games=home_total_games,
        away_total_games=away_total_games,
        set_scores=_tennis_set_scores(home_mapping, away_mapping),
        home_point=first_text(home_mapping, "point"),
        away_point=first_text(away_mapping, "point"),
        first_to_serve=first_to_serve,
        serving_side=_tennis_current_server(first_to_serve, home_total_games + away_total_games),
    )


def _soccer_state_from_payload(raw_event: Mapping[str, Any], *, raw_status: str) -> SoccerGameState | None:
    """提取 SofaScore 足球比赛节奏与卡牌状态。

    现阶段只稳定提取 period / clock_minutes / 卡牌；伤停补时若 payload 提供则带上。
    """

    time_payload = raw_event.get("time")
    time_mapping = time_payload if isinstance(time_payload, Mapping) else {}
    status_payload = raw_event.get("status")
    status_mapping = status_payload if isinstance(status_payload, Mapping) else {}
    period_code = str(status_mapping.get("code") or "").strip().lower()
    description = str(status_mapping.get("description") or "").strip().lower()
    period = _soccer_period_from(description, period_code, raw_status=raw_status)
    played_seconds = int_value(time_mapping.get("played"))
    clock_minutes = played_seconds // 60 if played_seconds is not None and played_seconds >= 0 else None
    added = int_value(time_mapping.get("injuryTime1")) or int_value(time_mapping.get("injuryTime2"))
    home_cards = raw_event.get("homeRedCards")
    away_cards = raw_event.get("awayRedCards")
    return SoccerGameState(
        period=period,
        clock_minutes=clock_minutes,
        added_minutes=added,
        home_red_cards=int_value(home_cards) or 0,
        away_red_cards=int_value(away_cards) or 0,
    )


def _soccer_period_from(description: str, period_code: str, *, raw_status: str) -> str | None:
    text = f"{description} {period_code} {raw_status}".lower()
    if "1st half" in text or "first half" in text or period_code == "1h":
        return "first_half"
    if "2nd half" in text or "second half" in text or period_code == "2h":
        return "second_half"
    if "extra time" in text or "et" in period_code:
        return "extra_time"
    if "penalt" in text:
        return "penalties"
    if "half time" in text or "halftime" in text:
        return "halftime"
    return None


def _baseball_state_from_payload(raw_event: Mapping[str, Any], *, raw_status: str) -> BaseballGameState:
    """提取 SofaScore 棒球局数。

    scheduled-events 当前只稳定提供 inning 文本和逐局比分，不提供出局数/垒包。
    策略层会继续把缺失出局数视为不能自动入场，但不再把 KBO live 市场误判成
    单纯的远期 endDate 拒绝。
    """

    normalized = raw_status.strip().lower()
    inning_half = None
    if re.search(r"\btop\b", normalized):
        inning_half = "top"
    elif re.search(r"\bbottom\b", normalized):
        inning_half = "bottom"
    current_inning = _baseball_current_inning(raw_event, raw_status=raw_status)
    return BaseballGameState(
        current_inning=current_inning,
        inning_half=inning_half,
        outs=None,
        offense_team=None,
        defense_team=None,
        occupied_bases=(),
    )


def _baseball_current_inning(raw_event: Mapping[str, Any], *, raw_status: str) -> int | None:
    match = re.search(r"(\d+)(?:st|nd|rd|th)?\s+inning", raw_status.strip().lower())
    if match:
        return int(match.group(1))
    innings: list[int] = []
    for score_key in ("homeScore", "awayScore"):
        score = raw_event.get(score_key)
        score_mapping = score if isinstance(score, Mapping) else {}
        for key, value in score_mapping.items():
            if value is None:
                continue
            period_match = re.fullmatch(r"period(\d+)", str(key))
            if period_match:
                innings.append(int(period_match.group(1)))
    return max(innings) if innings else None


def _tennis_serving_side(value: Any) -> str | None:
    side = int_value(value)
    if side == 1:
        return "home"
    if side == 2:
        return "away"
    return None


def _tennis_current_server(first_to_serve: str | None, total_games: int) -> str | None:
    """根据首个发球方和已完成/正在记录的总局数推算当前发球方。"""

    if first_to_serve not in {"home", "away"}:
        return None
    if total_games % 2 == 0:
        return first_to_serve
    return "away" if first_to_serve == "home" else "home"


def _tennis_current_set(
    raw_status: str,
    home_score: Mapping[str, Any],
    away_score: Mapping[str, Any],
) -> int | None:
    match = re.search(r"(\d+)(?:st|nd|rd|th)\s+set", raw_status.strip().lower())
    if match:
        return int(match.group(1))
    periods = [
        index
        for index in range(1, 6)
        if _period_score(home_score, index) is not None or _period_score(away_score, index) is not None
    ]
    return periods[-1] if periods else None


def _period_score(score: Mapping[str, Any], period: int | None) -> int | None:
    if period is None:
        return None
    return int_value(score.get(f"period{period}"))


def _period_total(score: Mapping[str, Any]) -> int:
    total = 0
    for index in range(1, 6):
        value = _period_score(score, index)
        if value is not None:
            total += value
    return total


def _tennis_set_scores(
    home_score: Mapping[str, Any],
    away_score: Mapping[str, Any],
) -> tuple[tuple[int, int], ...]:
    """返回 SofaScore 每盘局分，供 set winner 类盘口做确定性判断。"""

    scores: list[tuple[int, int]] = []
    for index in range(1, 6):
        home_games = _period_score(home_score, index)
        away_games = _period_score(away_score, index)
        if home_games is None and away_games is None:
            continue
        scores.append((home_games or 0, away_games or 0))
    return tuple(scores)


def _map_status(status: Mapping[str, Any]) -> SportsLiveGameStatus:
    status_type = str(status.get("type") or "").strip().lower()
    description = str(status.get("description") or "").strip().lower()
    text = f"{status_type} {description}"
    if "postpon" in text:
        return SportsLiveGameStatus.POSTPONED
    if "cancel" in text:
        return SportsLiveGameStatus.CANCELLED
    if any(token in text for token in ("retired", "walkover", "withdrawn")):
        return SportsLiveGameStatus.RETIRED
    if any(token in text for token in ("suspend", "delay", "interrupted")):
        return SportsLiveGameStatus.PAUSED
    if status_type == "inprogress":
        if any(token in description for token in ("halftime", "intermission", "pause", "break")):
            return SportsLiveGameStatus.PAUSED
        return SportsLiveGameStatus.LIVE
    if status_type == "finished":
        return SportsLiveGameStatus.ENDED
    if status_type == "notstarted":
        return SportsLiveGameStatus.SCHEDULED
    return SportsLiveGameStatus.UNKNOWN


def _seconds_remaining(
    *,
    sport: str,
    status: SportsLiveGameStatus,
    time_payload: Any,
) -> int | None:
    if status not in {SportsLiveGameStatus.LIVE, SportsLiveGameStatus.PAUSED}:
        return None
    if sport in {"baseball", "tennis"}:
        return None
    time_mapping = time_payload if isinstance(time_payload, Mapping) else {}
    played = int_value(time_mapping.get("played"))
    period_length = int_value(time_mapping.get("periodLength"))
    total_period_count = int_value(time_mapping.get("totalPeriodCount"))
    if played is None or period_length is None or total_period_count is None:
        return None
    if period_length <= 0 or total_period_count <= 0:
        return None
    return max(0, period_length * total_period_count - played)


def _period_label(*, sport: str, raw_status: str) -> str:
    text = raw_status.strip()
    normalized = text.lower()
    if sport == "basketball":
        match = re.search(r"(\d+)(?:st|nd|rd|th)\s+quarter", normalized)
        if match:
            return f"Q{match.group(1)}"
    if sport == "ice-hockey":
        match = re.search(r"(\d+)(?:st|nd|rd|th)\s+period", normalized)
        if match:
            return f"P{match.group(1)}"
    if sport == "american-football":
        match = re.search(r"(\d+)(?:st|nd|rd|th)\s+quarter", normalized)
        if match:
            return f"Q{match.group(1)}"
    if sport == "football":
        match = re.search(r"(\d+)(?:st|nd|rd|th)\s+half", normalized)
        if match:
            return f"H{match.group(1)}"
    if sport == "baseball":
        match = re.search(r"(\d+)(?:st|nd|rd|th)\s+inning", normalized)
        if match:
            return f"I{match.group(1)}"
    if sport == "tennis":
        match = re.search(r"(\d+)(?:st|nd|rd|th)\s+set", normalized)
        if match:
            return f"S{match.group(1)}"
    return text


def _league_name(tournament: Mapping[str, Any], *, sport: str) -> str:
    name = first_text(tournament, "name", "slug") or sport
    normalized = name.strip()
    if len(normalized) <= 5 and re.fullmatch(r"[A-Za-z0-9 ._-]+", normalized):
        return normalized.upper().replace("_", "-")
    return normalized


def _tournament_payload(raw_event: Mapping[str, Any]) -> Mapping[str, Any]:
    tournament = raw_event.get("tournament")
    tournament_mapping = tournament if isinstance(tournament, Mapping) else {}
    unique = tournament_mapping.get("uniqueTournament")
    if isinstance(unique, Mapping):
        return unique
    return tournament_mapping


def _event_matches_leagues(
    raw_event: Mapping[str, Any],
    allowed_aliases: set[str] | None,
) -> bool:
    if allowed_aliases is None:
        return True
    aliases = _event_tournament_aliases(raw_event)
    return bool(aliases.intersection(allowed_aliases))


def _allowed_tournament_aliases(league_codes: Sequence[str]) -> set[str] | None:
    codes = _normalize_codes(league_codes)
    if not codes:
        return None
    aliases: set[str] = set()
    has_wildcard = False
    for code in codes:
        values = _TOURNAMENT_ALIASES_BY_LEAGUE.get(code)
        if values is None and code in _TOURNAMENT_ALIASES_BY_LEAGUE:
            has_wildcard = True
            continue
        for value in values or ():
            aliases.add(_normalize_alias(value))
    if has_wildcard:
        return None
    return aliases


def _event_tournament_aliases(raw_event: Mapping[str, Any]) -> set[str]:
    tournament = raw_event.get("tournament")
    tournament_mapping = tournament if isinstance(tournament, Mapping) else {}
    unique = tournament_mapping.get("uniqueTournament")
    unique_mapping = unique if isinstance(unique, Mapping) else {}
    values = (
        first_text(tournament_mapping, "name"),
        first_text(tournament_mapping, "slug"),
        first_text(unique_mapping, "name"),
        first_text(unique_mapping, "slug"),
    )
    return {_normalize_alias(value) for value in values if value}


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


def _normalize_alias(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _slug_alias(value: str | None) -> str | None:
    text = _normalize_alias(value)
    if not text:
        return None
    return " ".join(text.split())


def _snapshot_age_seconds(snapshot: SportsLiveSnapshot, observed_at: datetime) -> float:
    age = observed_at - snapshot.observed_at
    return max(0.0, age.total_seconds())
