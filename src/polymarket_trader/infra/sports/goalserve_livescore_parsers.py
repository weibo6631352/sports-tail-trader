"""Goalserve getfeed livescore per-sport parsers。

getfeed 与 inplay feed 的关键区别：
  - 需要 API key（URL 中），非 IP 白名单
  - 状态用文字描述（"In Progress"/"Finished" 等），非 core flags
  - JSON 类运动响应有统一的顶层 {"scores": {...}} 包装
  - XML 类运动（basketball/baseball/hockey/tennis）通过客户端转换为相同 dict 结构

scores 内结构按运动而异：
    team 类（handball/rugby/boxing/mma/basketball/baseball/hockey/tennis）: scores.category[].match[]
    golf: scores.tournament[].player[]
    horse_racing: scores.tournament[].race[]
    motogp: scores.tournament[].{race/first_practice/...}.results.driver[]
    f1: scores=null 时无数据（赛间窗口）

状态归一规则：
  "In Progress" / "Inprogress" / "Live" → LIVE
  "Finished" / "Final" / "FT"           → ENDED
  "Not Started"                          → SCHEDULED
  XML 运动状态（"1st Quarter"/"3rd Period"/"Inning 8"/"1st Set"）→ LIVE
  其他                                   → UNKNOWN
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from polymarket_trader.domain.sports_live import (
    BaseballGameState,
    CricketGameState,
    HandballGameState,
    LiveEvent,
    LiveEventKind,
    MMAFightState,
    Participant,
    RaceState,
    RugbyGameState,
    SportsLiveGameStatus,
    TennisGameState,
)
from polymarket_trader.infra.sports.common import utc_now


def _split_team_name(full_name: str) -> tuple[str | None, str | None]:
    """拆分 "[City] [Nickname]" 格式的队名，返回 (location, team_nickname)。

    用于 NBA/MLB/NHL 等队名中城市前缀拆分，生成简写别名（如 "Knicks"）。
    单词队名（无空格）直接返回 (None, full_name)。
    """
    parts = full_name.strip().split()
    if len(parts) < 2:
        return None, full_name if parts else None
    return " ".join(parts[:-1]), parts[-1]


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------


def _text_status(raw: Any) -> SportsLiveGameStatus:
    s = str(raw or "").strip().lower()
    if s in ("in progress", "inprogress", "live"):
        return SportsLiveGameStatus.LIVE
    if s in ("finished", "final", "ft"):
        return SportsLiveGameStatus.ENDED
    if s == "not started":
        return SportsLiveGameStatus.SCHEDULED
    return SportsLiveGameStatus.UNKNOWN


def _int_val(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _str_val(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _iter_matches(scores: dict[str, Any]) -> list[dict[str, Any]]:
    """从 scores.category[].match[] 展开所有比赛，容忍 category 为 dict 或 list。

    Goalserve livescore 用 ``category`` 字段（非 SofaScore 的 ``categories``），
    每个 category 下 ``match`` 可以是单个 dict 或 list。
    """
    matches: list[dict[str, Any]] = []
    categories = scores.get("category") or []
    if isinstance(categories, dict):
        categories = [categories]
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        raw = cat.get("match") or []
        if isinstance(raw, dict):
            raw = [raw]
        for m in raw:
            if isinstance(m, dict):
                matches.append(m)
    return matches


# ---------------------------------------------------------------------------
# Cricket
# ---------------------------------------------------------------------------

_CRICKET_BATTING_MAP = {"1": "home", "2": "away"}


def _parse_cricket(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    for match in _iter_matches(scores):
        event_id = _str_val(match.get("id")) or _str_val(match.get("matchid"))
        if not event_id:
            continue
        status = _text_status(match.get("status"))
        home_team = match.get("localteam") or {}
        away_team = match.get("awayteam") or {}
        home_name = _str_val(home_team.get("name"))
        away_name = _str_val(away_team.get("name"))
        home_score = _int_val(home_team.get("totalscore") or home_team.get("score"))
        away_score = _int_val(away_team.get("totalscore") or away_team.get("score"))
        league = _str_val(match.get("competition") or match.get("league") or "")

        # overs 格式 "12.3" → completed=12, balls_in_over=3
        time_raw = _str_val(match.get("time") or match.get("overs") or "")
        overs_completed: int | None = None
        balls_in_over: int | None = None
        if "." in time_raw:
            parts = time_raw.split(".", 1)
            overs_completed = _int_val(parts[0])
            balls_in_over = _int_val(parts[1])
        else:
            overs_completed = _int_val(time_raw) if time_raw else None

        innings_list = match.get("innings") or []
        if isinstance(innings_list, dict):
            innings_list = list(innings_list.values())
        current_innings: int | None = None
        batting_side: str | None = None
        runs: int | None = None
        wickets: int | None = None
        if isinstance(innings_list, list) and innings_list:
            last = innings_list[-1]
            if isinstance(last, dict):
                current_innings = _int_val(last.get("number") or last.get("innings_number"))
                batting_team_id = _str_val(last.get("batting_team") or last.get("team_id") or "")
                batting_side = _CRICKET_BATTING_MAP.get(batting_team_id)
                runs = _int_val(last.get("runs") or last.get("score"))
                wickets = _int_val(last.get("wickets"))

        cricket_state = CricketGameState(
            current_innings=current_innings,
            batting_side=batting_side,
            runs=runs,
            wickets=wickets,
            overs_completed=overs_completed,
            balls_in_over=balls_in_over,
        )
        events.append(
            LiveEvent(
                source="goalserve_livescore",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=league,
                sport="cricket",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=time_raw,
                raw_status=_str_val(match.get("status")),
                observed_at=observed_at,
                external_ids={"goalserve": event_id},
                cricket_state=cricket_state,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Handball
# ---------------------------------------------------------------------------

_HALF_PERIOD_MAP: dict[str, str] = {
    "1st half": "first_half",
    "first half": "first_half",
    "2nd half": "second_half",
    "second half": "second_half",
    "extra time": "extra_time",
    "half time": "first_half",
    "halftime": "first_half",
}


def _parse_handball(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    for match in _iter_matches(scores):
        event_id = _str_val(match.get("id") or match.get("matchid") or "")
        if not event_id:
            continue
        status = _text_status(match.get("status"))
        home_team = match.get("localteam") or {}
        away_team = match.get("awayteam") or {}
        home_name = _str_val(home_team.get("name"))
        away_name = _str_val(away_team.get("name"))
        # totalscore = full-game score; t1/t2 = per-half scores
        home_score = _int_val(home_team.get("totalscore") or home_team.get("score"))
        away_score = _int_val(away_team.get("totalscore") or away_team.get("score"))
        home_p1 = _int_val(home_team.get("t1"))
        away_p1 = _int_val(away_team.get("t1"))

        period_raw = _str_val(match.get("status_str") or match.get("period") or "")
        handball_period = _HALF_PERIOD_MAP.get(period_raw.lower())
        # time field in Goalserve is scheduled kickoff time (HH:MM), not game clock
        clock_minutes: int | None = None

        handball_state = HandballGameState(
            period=handball_period,
            clock_minutes=clock_minutes,
            home_period1=home_p1,
            away_period1=away_p1,
        )
        league = _str_val(
            (match.get("category") or {}).get("name")
            if isinstance(match.get("category"), dict)
            else (match.get("league") or match.get("competition") or "")
        )
        events.append(
            LiveEvent(
                source="goalserve_livescore",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=league,
                sport="handball",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=period_raw,
                raw_status=_str_val(match.get("status")),
                observed_at=observed_at,
                external_ids={"goalserve": event_id},
                handball_state=handball_state,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Rugby
# ---------------------------------------------------------------------------


def _parse_rugby(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    for match in _iter_matches(scores):
        event_id = _str_val(match.get("id") or match.get("matchid") or "")
        if not event_id:
            continue
        status = _text_status(match.get("status"))
        home_team = match.get("localteam") or {}
        away_team = match.get("awayteam") or {}
        home_name = _str_val(home_team.get("name"))
        away_name = _str_val(away_team.get("name"))
        home_score = _int_val(home_team.get("totalscore") or home_team.get("score"))
        away_score = _int_val(away_team.get("totalscore") or away_team.get("score"))
        home_p1 = _int_val(home_team.get("t1"))
        away_p1 = _int_val(away_team.get("t1"))

        period_raw = _str_val(match.get("status_str") or match.get("period") or "")
        rugby_period = _HALF_PERIOD_MAP.get(period_raw.lower())

        rugby_state = RugbyGameState(
            period=rugby_period,
            clock_minutes=None,
            home_period1=home_p1,
            away_period1=away_p1,
        )
        league = _str_val(match.get("league") or match.get("competition") or "")
        events.append(
            LiveEvent(
                source="goalserve_livescore",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=league,
                sport="rugby",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=period_raw,
                raw_status=_str_val(match.get("status")),
                observed_at=observed_at,
                external_ids={"goalserve": event_id},
                rugby_state=rugby_state,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Boxing
# ---------------------------------------------------------------------------


def _parse_boxing(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    for match in _iter_matches(scores):
        event_id = _str_val(match.get("id") or match.get("matchid") or "")
        if not event_id:
            continue
        status = _text_status(match.get("status"))
        home_team = match.get("localteam") or {}
        away_team = match.get("awayteam") or {}
        home_name = _str_val(home_team.get("name"))
        away_name = _str_val(away_team.get("name"))
        league = _str_val(match.get("league") or match.get("competition") or "")

        current_round = _int_val(match.get("round"))
        total_rounds = _int_val(match.get("total_rounds") or match.get("rounds"))

        winner_side: str | None = None
        home_winner = _str_val(home_team.get("winner")).lower()
        away_winner = _str_val(away_team.get("winner")).lower()
        if home_winner in ("yes", "1", "true"):
            winner_side = "home"
        elif away_winner in ("yes", "1", "true"):
            winner_side = "away"

        mma_state = MMAFightState(
            current_round=current_round,
            total_rounds=total_rounds,
            winner_side=winner_side,
        )
        events.append(
            LiveEvent(
                source="goalserve_livescore",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=league,
                sport="boxing",
                participants=(
                    Participant(role="home", name=home_name, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, external_ids={"goalserve": event_id}),
                ),
                status=status,
                raw_status=_str_val(match.get("status")),
                observed_at=observed_at,
                external_ids={"goalserve": event_id},
                mma_state=mma_state,
            )
        )
    return events


# ---------------------------------------------------------------------------
# MMA
# ---------------------------------------------------------------------------


def _parse_mma(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """MMA feed: scores.category[].match[] 结构（category 可以是 dict 或 list）。"""
    events: list[LiveEvent] = []
    for match in _iter_matches(scores):
        event_id = _str_val(match.get("id") or "")
        if not event_id:
            continue
        status = _text_status(match.get("status"))
        home_team = match.get("localteam") or {}
        away_team = match.get("awayteam") or {}
        home_name = _str_val(home_team.get("name") if isinstance(home_team, dict) else "")
        away_name = _str_val(away_team.get("name") if isinstance(away_team, dict) else "")

        current_round = _int_val(match.get("round"))
        total_rounds = _int_val(match.get("total_rounds"))
        time_left = _str_val(match.get("time_left") or "")

        win_result = match.get("win_result") or {}
        result_method: str | None = None
        winner_side: str | None = None
        if isinstance(win_result, dict):
            result_method = _str_val(win_result.get("won_by") or "") or None
            winner_raw = _str_val(win_result.get("winner") or win_result.get("side") or "")
            if winner_raw in ("home", "localteam"):
                winner_side = "home"
            elif winner_raw in ("away", "awayteam"):
                winner_side = "away"

        # winner 字段在 localteam/awayteam 上（"True"/"False" 字符串）
        if winner_side is None:
            home_winner = _str_val(home_team.get("winner") if isinstance(home_team, dict) else "").lower()
            away_winner = _str_val(away_team.get("winner") if isinstance(away_team, dict) else "").lower()
            if home_winner in ("yes", "1", "true"):
                winner_side = "home"
            elif away_winner in ("yes", "1", "true"):
                winner_side = "away"

        mma_state = MMAFightState(
            current_round=current_round,
            total_rounds=total_rounds,
            time_in_round=time_left or None,
            result_method=result_method,
            winner_side=winner_side,
        )
        events.append(
            LiveEvent(
                source="goalserve_livescore",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league="",
                sport="mma",
                participants=(
                    Participant(role="home", name=home_name, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, external_ids={"goalserve": event_id}),
                ),
                status=status,
                raw_status=_str_val(match.get("status")),
                observed_at=observed_at,
                external_ids={"goalserve": event_id},
                mma_state=mma_state,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Golf
# ---------------------------------------------------------------------------


def _golf_position(raw: Any) -> int | None:
    """解析高尔夫位次，处理 "T1"（并列）→ 1 的格式。"""
    s = str(raw or "").strip().lstrip("T").lstrip("t")
    return _int_val(s) if s else None


def _parse_golf(sport_key: str, scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """高尔夫锦标赛 leaderboard：每个 tournament 是一个 LiveEvent（TOURNAMENT_FIELD）。"""
    events: list[LiveEvent] = []
    tournaments = scores.get("tournament") or []
    if isinstance(tournaments, dict):
        tournaments = [tournaments]

    for tournament in tournaments:
        if not isinstance(tournament, dict):
            continue
        event_id = _str_val(tournament.get("id") or "")
        if not event_id:
            continue
        name = _str_val(tournament.get("name") or "")
        status = _text_status(tournament.get("status") or "")

        players_raw = tournament.get("player") or []
        if isinstance(players_raw, dict):
            players_raw = [players_raw]

        participants: list[Participant] = []
        for player in players_raw:
            if not isinstance(player, dict):
                continue
            participants.append(
                Participant(
                    role="player",
                    name=_str_val(player.get("name") or ""),
                    position=_golf_position(player.get("pos")),
                    external_ids={"goalserve": _str_val(player.get("id") or "")},
                    team=_str_val(player.get("country") or ""),
                )
            )

        events.append(
            LiveEvent(
                source="goalserve_livescore",
                source_event_id=event_id,
                kind=LiveEventKind.TOURNAMENT_FIELD,
                league=_str_val(tournament.get("type") or ""),
                sport=sport_key,
                participants=tuple(participants),
                status=status,
                event_name=name,
                raw_status=_str_val(tournament.get("status") or ""),
                observed_at=observed_at,
                external_ids={"goalserve": event_id},
            )
        )
    return events


# ---------------------------------------------------------------------------
# Horse Racing
# ---------------------------------------------------------------------------


def _parse_horse_racing(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """赛马：scores.tournament[].race[]，每场 race 是一个 RACE LiveEvent。

    position 用马的出发号码（number）而非比赛结果名次（仅在 results 字段出现且比赛
    已结束时才有名次）。status 从 results 字段推断：null → SCHEDULED，否则 ENDED。
    """
    events: list[LiveEvent] = []
    venues = scores.get("tournament") or []
    if isinstance(venues, dict):
        venues = [venues]

    for venue in venues:
        if not isinstance(venue, dict):
            continue
        venue_name = _str_val(venue.get("name") or "")
        races = venue.get("race") or []
        if isinstance(races, dict):
            races = [races]

        for race in races:
            if not isinstance(race, dict):
                continue
            event_id = _str_val(race.get("id") or "")
            if not event_id:
                continue
            name = _str_val(race.get("name") or "")
            # results=None 表示尚未开跑；有值表示已结束
            status = (
                SportsLiveGameStatus.SCHEDULED
                if race.get("results") is None
                else SportsLiveGameStatus.ENDED
            )

            runners_raw = race.get("runners") or {}
            horses = runners_raw.get("horse", []) if isinstance(runners_raw, dict) else []
            if isinstance(horses, dict):
                horses = [horses]

            participants: list[Participant] = []
            for horse in horses:
                if not isinstance(horse, dict):
                    continue
                participants.append(
                    Participant(
                        role="driver",
                        name=_str_val(horse.get("name") or ""),
                        position=_int_val(horse.get("number")),  # 出发号，非名次
                        team=_str_val(horse.get("jockey") or ""),
                        external_ids={"goalserve": _str_val(horse.get("id") or "")},
                    )
                )

            events.append(
                LiveEvent(
                    source="goalserve_livescore",
                    source_event_id=event_id,
                    kind=LiveEventKind.RACE,
                    league=venue_name,
                    sport="horse_racing",
                    participants=tuple(participants),
                    status=status,
                    event_name=name,
                    raw_status=None,
                    observed_at=observed_at,
                    external_ids={"goalserve": event_id},
                )
            )
    return events


# ---------------------------------------------------------------------------
# F1 / MotoGP
# ---------------------------------------------------------------------------

_MOTOGP_SESSION_KEYS = (
    "race",
    "first_qualification",
    "second_qualification",
    "third_practice",
    "first_practice",
    "second_practice",
    "qualifying",
)


def _parse_motorsport(sport_key: str, scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """赛车：scores.tournament[].{session_type}.results.driver[]。

    MotoGP/F1 每个 tournament 包含多种 session（race, 练习赛, 排位赛）；
    每种 session 的 results.driver[] 包含按位次排列的车手数据。
    按 tournament × session 各产生一个 RACE LiveEvent。
    """
    events: list[LiveEvent] = []
    tournaments = scores.get("tournament") or []
    if isinstance(tournaments, dict):
        tournaments = [tournaments]

    for t in tournaments:
        if not isinstance(t, dict):
            continue
        t_name = _str_val(t.get("name") or "")
        t_id = _str_val(t.get("id") or "")

        for session_key in _MOTOGP_SESSION_KEYS:
            session = t.get(session_key)
            if not session:
                continue
            sessions = session if isinstance(session, list) else [session]

            for sess_idx, sess in enumerate(sessions):
                if not isinstance(sess, dict):
                    continue
                status_raw = _str_val(sess.get("status") or "")
                status = _text_status(status_raw)

                results = sess.get("results") or {}
                drivers_raw = results.get("driver") if isinstance(results, dict) else []
                if isinstance(drivers_raw, dict):
                    drivers_raw = [drivers_raw]

                participants: list[Participant] = []
                for driver in (drivers_raw or []):
                    if not isinstance(driver, dict):
                        continue
                    pos = _int_val(driver.get("pos") or driver.get("position"))
                    participants.append(
                        Participant(
                            role="driver",
                            name=_str_val(driver.get("name") or ""),
                            position=pos,
                            team=_str_val(driver.get("team") or ""),
                            external_ids={"goalserve": _str_val(driver.get("driver_id") or "")},
                        )
                    )

                # event_id 拼接：tournament_id + session类型 [+ 序号（多场race时）]
                suffix = f"_{sess_idx}" if sess_idx > 0 else ""
                event_id = f"{t_id}_{session_key}{suffix}"

                race_state = RaceState(
                    laps_completed=_int_val(sess.get("laps_running")),
                    total_laps=_int_val(sess.get("total_laps")),
                    status_flag=status_raw or None,
                )
                events.append(
                    LiveEvent(
                        source="goalserve_livescore",
                        source_event_id=event_id,
                        kind=LiveEventKind.RACE,
                        league=t_name,
                        sport=sport_key,
                        participants=tuple(participants),
                        status=status,
                        event_name=f"{t_name} {session_key.replace('_', ' ')}",
                        raw_status=status_raw or None,
                        observed_at=observed_at,
                        external_ids={"goalserve": t_id},
                        race_state=race_state,
                    )
                )
    return events


# ---------------------------------------------------------------------------
# Basketball（NBA / 国际篮球）
# ---------------------------------------------------------------------------

# Goalserve XML 状态 → 归一状态
_XML_LIVE_STATUSES = frozenset({
    "1st quarter", "2nd quarter", "3rd quarter", "4th quarter",
    "overtime", "ot",
    "1st period", "2nd period", "3rd period",
    "inning 1", "inning 2", "inning 3", "inning 4", "inning 5",
    "inning 6", "inning 7", "inning 8", "inning 9", "extra innings",
    "top 1st", "bot 1st", "top 2nd", "bot 2nd", "top 3rd", "bot 3rd",
    "top 4th", "bot 4th", "top 5th", "bot 5th", "top 6th", "bot 6th",
    "top 7th", "bot 7th", "top 8th", "bot 8th", "top 9th", "bot 9th",
    "1st set", "2nd set", "3rd set", "4th set", "5th set",
    "1st half", "2nd half", "halftime",
    "delayed",
})
_XML_ENDED_STATUSES = frozenset({
    "finished", "final", "ft", "after over time", "after et", "after pen.",
    "aet", "pen", "aps",
})
_XML_SCHED_STATUSES = frozenset({"not started", "ns", ""})


def _xml_status(raw: Any) -> SportsLiveGameStatus:
    s = str(raw or "").strip().lower()
    if s in _XML_ENDED_STATUSES:
        return SportsLiveGameStatus.ENDED
    if s in _XML_SCHED_STATUSES:
        return SportsLiveGameStatus.SCHEDULED
    if s in _XML_LIVE_STATUSES or any(kw in s for kw in ("quarter", "period", "inning", "set", "bottom")):
        return SportsLiveGameStatus.LIVE
    return SportsLiveGameStatus.UNKNOWN


def _basketball_seconds_remaining(status: str, timer_raw: Any) -> int | None:
    """NBA/FIBA 剩余秒数估算（timer = 已打分钟数）。

    NBA: 4×12 min；FIBA: 4×10 min，但我们对所有篮球赛用12分钟近似（更保守）。
    """
    period_minutes = 12
    s = status.lower()
    try:
        elapsed = int(float(str(timer_raw or "").strip()))
    except (ValueError, TypeError):
        elapsed = 0
    remaining_in_period = max(0, period_minutes - elapsed) * 60
    if "1st quarter" in s:
        return 3 * period_minutes * 60 + remaining_in_period
    if "2nd quarter" in s:
        return 2 * period_minutes * 60 + remaining_in_period
    if "3rd quarter" in s:
        return period_minutes * 60 + remaining_in_period
    if "4th quarter" in s:
        return remaining_in_period
    if "overtime" in s or s == "ot":
        return max(0, 5 * 60 - elapsed * 60)
    return None


def _hockey_seconds_remaining(status: str, timer_raw: Any) -> int | None:
    """NHL/IIHF 剩余秒数估算（timer = 已打分钟数）。

    3×20 min 正常时间；加时赛 5 min (NHL) / 20 min (IIHF)。
    """
    period_minutes = 20
    s = status.lower()
    try:
        elapsed = int(str(timer_raw or "").strip())
    except (ValueError, TypeError):
        elapsed = 0
    remaining_in_period = max(0, period_minutes - elapsed) * 60
    if "1st period" in s:
        return 2 * period_minutes * 60 + remaining_in_period
    if "2nd period" in s:
        return period_minutes * 60 + remaining_in_period
    if "3rd period" in s:
        return remaining_in_period
    if "overtime" in s or " ot" in s:
        return max(0, 5 * 60 - elapsed * 60)
    # "After Over Time" → 刚结束加时，0秒
    return None


def _parse_basketball(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    for match in _iter_matches(scores):
        event_id = _str_val(match.get("id") or match.get("matchid") or "")
        if not event_id:
            continue
        status_raw = _str_val(match.get("status"))
        status = _xml_status(status_raw)
        home_team = match.get("localteam") or {}
        away_team = match.get("awayteam") or {}
        home_name = _str_val(home_team.get("name"))
        away_name = _str_val(away_team.get("name"))
        home_score = _int_val(home_team.get("totalscore"))
        away_score = _int_val(away_team.get("totalscore"))
        cat_name = _str_val(match.get("name") or "")
        timer_raw = match.get("timer")
        seconds_remaining = _basketball_seconds_remaining(status_raw, timer_raw) if status == SportsLiveGameStatus.LIVE else None
        events.append(
            LiveEvent(
                source="goalserve_livescore",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=cat_name,
                sport="basketball",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=status_raw,
                seconds_remaining=seconds_remaining,
                raw_status=status_raw,
                observed_at=observed_at,
                external_ids={"goalserve": event_id},
            )
        )
    return events


def _parse_basketball_with_cats(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """从 category 结构解析 basketball，category.name 作为 league。"""
    events: list[LiveEvent] = []
    categories = scores.get("category") or []
    if isinstance(categories, dict):
        categories = [categories]
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        cat_name = _str_val(cat.get("name"))
        raw = cat.get("match") or []
        if isinstance(raw, dict):
            raw = [raw]
        for match in raw:
            if not isinstance(match, dict):
                continue
            event_id = _str_val(match.get("id") or match.get("matchid") or "")
            if not event_id:
                continue
            status_raw = _str_val(match.get("status"))
            status = _xml_status(status_raw)
            # basketball/home uses localteam; bsktbl/nba-scores uses hometeam
            home_team = match.get("localteam") or match.get("hometeam") or {}
            away_team = match.get("awayteam") or {}
            home_name = _str_val(home_team.get("name"))
            away_name = _str_val(away_team.get("name"))
            home_score = _int_val(home_team.get("totalscore"))
            away_score = _int_val(away_team.get("totalscore"))
            home_loc, home_nick = _split_team_name(home_name)
            away_loc, away_nick = _split_team_name(away_name)
            timer_raw = match.get("timer")
            seconds_remaining = _basketball_seconds_remaining(status_raw, timer_raw) if status == SportsLiveGameStatus.LIVE else None
            events.append(
                LiveEvent(
                    source="goalserve_livescore",
                    source_event_id=event_id,
                    kind=LiveEventKind.TEAM_MATCH,
                    league=cat_name,
                    sport="basketball",
                    participants=(
                        Participant(role="home", name=home_name, score=home_score, location=home_loc, team=home_nick, external_ids={"goalserve": event_id}),
                        Participant(role="away", name=away_name, score=away_score, location=away_loc, team=away_nick, external_ids={"goalserve": event_id}),
                    ),
                    status=status,
                    period=status_raw,
                    seconds_remaining=seconds_remaining,
                    raw_status=status_raw,
                    observed_at=observed_at,
                    external_ids={"goalserve": event_id},
                )
            )
    return events


# ---------------------------------------------------------------------------
# Hockey（NHL / 国际冰球）
# ---------------------------------------------------------------------------


def _parse_hockey_with_cats(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """从 category 结构解析 hockey，category.name 作为 league。"""
    events: list[LiveEvent] = []
    categories = scores.get("category") or []
    if isinstance(categories, dict):
        categories = [categories]
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        cat_name = _str_val(cat.get("name"))
        raw = cat.get("match") or []
        if isinstance(raw, dict):
            raw = [raw]
        for match in raw:
            if not isinstance(match, dict):
                continue
            event_id = _str_val(match.get("id") or match.get("matchid") or "")
            if not event_id:
                continue
            status_raw = _str_val(match.get("status"))
            status = _xml_status(status_raw)
            # hockey/home uses localteam; hockey/nhl-scores uses hometeam
            home_team = match.get("localteam") or match.get("hometeam") or {}
            away_team = match.get("awayteam") or {}
            home_name = _str_val(home_team.get("name"))
            away_name = _str_val(away_team.get("name"))
            home_score = _int_val(home_team.get("totalscore"))
            away_score = _int_val(away_team.get("totalscore"))
            home_loc, home_nick = _split_team_name(home_name)
            away_loc, away_nick = _split_team_name(away_name)
            timer_raw = match.get("timer")
            seconds_remaining = _hockey_seconds_remaining(status_raw, timer_raw) if status == SportsLiveGameStatus.LIVE else None
            events.append(
                LiveEvent(
                    source="goalserve_livescore",
                    source_event_id=event_id,
                    kind=LiveEventKind.TEAM_MATCH,
                    league=cat_name,
                    sport="hockey",
                    participants=(
                        Participant(role="home", name=home_name, score=home_score, location=home_loc, team=home_nick, external_ids={"goalserve": event_id}),
                        Participant(role="away", name=away_name, score=away_score, location=away_loc, team=away_nick, external_ids={"goalserve": event_id}),
                    ),
                    status=status,
                    period=status_raw,
                    seconds_remaining=seconds_remaining,
                    raw_status=status_raw,
                    observed_at=observed_at,
                    external_ids={"goalserve": event_id},
                )
            )
    return events


# ---------------------------------------------------------------------------
# Baseball（MLB / 国际棒球）
# ---------------------------------------------------------------------------


def _baseball_state_from_match(match: dict[str, Any]) -> BaseballGameState:
    """从 XML 转换后的 dict 提取 BaseballGameState。

    status 示例："Inning 8"/"Top 9th"/"Bot 4th"
    localteam/awayteam 有 in1..in9 属性。
    """
    status_raw = _str_val(match.get("status")).lower()
    inning: int | None = None
    half: str | None = None
    for i in range(1, 13):
        if (
            f" {i} " in f" {status_raw} "
            or f"{i}th" in status_raw
            or f"{i}nd" in status_raw
            or f"{i}st" in status_raw
            or f"{i}rd" in status_raw
        ):
            inning = i
            break
    if "top" in status_raw:
        half = "top"
    elif "bot" in status_raw or "bottom" in status_raw:
        half = "bottom"
    return BaseballGameState(
        current_inning=inning,
        inning_half=half,
    )


def _baseball_seconds_remaining(status_raw: str) -> int | None:
    """棒球剩余秒数粗估：基于局数和上下半局，每局约20分钟。"""
    s = status_raw.lower()
    inning: int = 9
    for i in range(1, 13):
        if (
            f" {i} " in f" {s} "
            or f"{i}th" in s
            or f"{i}nd" in s
            or f"{i}st" in s
            or f"{i}rd" in s
        ):
            inning = i
            break
    if "extra" in s or inning > 9:
        return 600
    remaining_innings = max(0, 9 - inning)
    # top half = 1 full inning remaining in this inning + later
    if "top" in s or not ("bot" in s or "bottom" in s):
        remaining_halfings = remaining_innings * 2 + 1
    else:
        remaining_halfings = remaining_innings * 2
    return remaining_halfings * 600


def _parse_baseball_with_cats(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """从 category 结构解析 baseball，category.name 作为 league。"""
    events: list[LiveEvent] = []
    categories = scores.get("category") or []
    if isinstance(categories, dict):
        categories = [categories]
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        cat_name = _str_val(cat.get("name"))
        raw = cat.get("match") or []
        if isinstance(raw, dict):
            raw = [raw]
        for match in raw:
            if not isinstance(match, dict):
                continue
            event_id = _str_val(match.get("id") or match.get("matchid") or "")
            if not event_id:
                continue
            status_raw = _str_val(match.get("status"))
            status = _xml_status(status_raw)
            # baseball/home uses localteam; baseball/mlb-scores uses hometeam
            home_team = match.get("localteam") or match.get("hometeam") or {}
            away_team = match.get("awayteam") or {}
            home_name = _str_val(home_team.get("name"))
            away_name = _str_val(away_team.get("name"))
            home_score = _int_val(home_team.get("totalscore"))
            away_score = _int_val(away_team.get("totalscore"))
            home_loc, home_nick = _split_team_name(home_name)
            away_loc, away_nick = _split_team_name(away_name)
            baseball_state = _baseball_state_from_match(match) if status == SportsLiveGameStatus.LIVE else None
            seconds_remaining = _baseball_seconds_remaining(status_raw) if status == SportsLiveGameStatus.LIVE else None
            events.append(
                LiveEvent(
                    source="goalserve_livescore",
                    source_event_id=event_id,
                    kind=LiveEventKind.TEAM_MATCH,
                    league=cat_name,
                    sport="baseball",
                    participants=(
                        Participant(role="home", name=home_name, score=home_score, location=home_loc, team=home_nick, external_ids={"goalserve": event_id}),
                        Participant(role="away", name=away_name, score=away_score, location=away_loc, team=away_nick, external_ids={"goalserve": event_id}),
                    ),
                    status=status,
                    period=status_raw,
                    seconds_remaining=seconds_remaining,
                    raw_status=status_raw,
                    observed_at=observed_at,
                    external_ids={"goalserve": event_id},
                    baseball_state=baseball_state,
                )
            )
    return events


# ---------------------------------------------------------------------------
# Tennis（ATP / WTA）
# ---------------------------------------------------------------------------


def _tennis_state_from_players(players: list[dict[str, Any]]) -> TennisGameState | None:
    """从 tennis_scores/home XML 的 player 列表提取 TennisGameState。

    tennis_scores/home 格式：每 match 下两个 <player> 元素，totalscore=赢盘数，s1..s5=各盘得分。
    """
    if len(players) < 2:
        return None
    p0, p1 = players[0], players[1]
    home_sets = _int_val(p0.get("totalscore")) or 0
    away_sets = _int_val(p1.get("totalscore")) or 0
    set_scores: list[tuple[int, int]] = []
    for i in range(1, 6):
        hs_raw = p0.get(f"s{i}", "")
        as_raw = p1.get(f"s{i}", "")
        # strip tiebreak suffix (e.g. "7.10" → 7)
        def _set_int(v: Any) -> int | None:
            s = str(v or "").split(".")[0]
            try:
                return int(s) if s else None
            except ValueError:
                return None
        hs = _set_int(hs_raw)
        as_ = _set_int(as_raw)
        if hs is not None and as_ is not None:
            set_scores.append((hs, as_))
        else:
            break
    current_set = len(set_scores) + 1 if set_scores else 1
    home_cur = _int_val(p0.get(f"s{current_set}", "").split(".")[0]) if current_set <= 5 else None
    away_cur = _int_val(p1.get(f"s{current_set}", "").split(".")[0]) if current_set <= 5 else None
    return TennisGameState(
        home_sets_won=home_sets,
        away_sets_won=away_sets,
        current_set=current_set if current_set <= 5 else None,
        home_current_set_games=home_cur,
        away_current_set_games=away_cur,
        set_scores=tuple(set_scores),
    )


def _parse_tennis_with_cats(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """从 tennis_scores/home category 结构解析 tennis。

    tennis_scores/home XML 每 match 下有两个 <player> 元素（非 localteam/awayteam）。
    _xml_to_livescore_dict 将多个同名子节点转换为 _player_list。
    """
    events: list[LiveEvent] = []
    categories = scores.get("category") or []
    if isinstance(categories, dict):
        categories = [categories]
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        cat_name = _str_val(cat.get("name"))
        raw = cat.get("match") or []
        if isinstance(raw, dict):
            raw = [raw]
        for match in raw:
            if not isinstance(match, dict):
                continue
            event_id = _str_val(match.get("id") or match.get("matchid") or "")
            if not event_id:
                continue
            status_raw = _str_val(match.get("status"))
            status = _text_status(status_raw)
            # tennis_scores/home uses <player> tags → stored as _player_list by xml converter
            players: list[dict[str, Any]] = match.get("_player_list") or []
            if len(players) < 2:
                continue
            home_name = _str_val(players[0].get("name"))
            away_name = _str_val(players[1].get("name"))
            home_sets = _int_val(players[0].get("totalscore")) or 0
            away_sets = _int_val(players[1].get("totalscore")) or 0
            tennis_state = _tennis_state_from_players(players) if status == SportsLiveGameStatus.LIVE else None
            events.append(
                LiveEvent(
                    source="goalserve_livescore",
                    source_event_id=event_id,
                    kind=LiveEventKind.TEAM_MATCH,
                    league=cat_name,
                    sport="tennis",
                    participants=(
                        Participant(role="home", name=home_name, score=home_sets, external_ids={"goalserve": event_id}),
                        Participant(role="away", name=away_name, score=away_sets, external_ids={"goalserve": event_id}),
                    ),
                    status=status,
                    period=status_raw,
                    raw_status=status_raw,
                    observed_at=observed_at,
                    external_ids={"goalserve": event_id},
                    tennis_state=tennis_state,
                )
            )
    return events


# ---------------------------------------------------------------------------
# 顶层分派
# ---------------------------------------------------------------------------


def parse_goalserve_livescore_sport(
    sport: str,
    data: dict[str, Any],
    observed_at: datetime | None = None,
) -> list[LiveEvent]:
    """顶层分派：按 sport key 调对应 livescore parser，返回 LiveEvent 列表。

    data 为 getfeed 根节点，统一格式：{"scores": {category: [...]}}。
    XML 运动（basketball/baseball/hockey/tennis）由客户端预转换为此格式。
    scores=null 时（F1 赛间等）直接返回空列表。
    observed_at 为 None 时自动取当前 UTC 时间。
    """
    ts = observed_at or utc_now()
    if not isinstance(data, dict):
        return []
    scores = data.get("scores")
    if not isinstance(scores, dict):
        return []

    match sport:
        case "basketball" | "nba" | "wnba":
            return _parse_basketball_with_cats(scores, ts)
        case "hockey" | "nhl":
            return _parse_hockey_with_cats(scores, ts)
        case "baseball" | "mlb":
            return _parse_baseball_with_cats(scores, ts)
        case "tennis":
            return _parse_tennis_with_cats(scores, ts)
        case "cricket":
            return _parse_cricket(scores, ts)
        case "handball":
            return _parse_handball(scores, ts)
        case "rugby":
            return _parse_rugby(scores, ts)
        case "boxing":
            return _parse_boxing(scores, ts)
        case "mma":
            return _parse_mma(scores, ts)
        case "golf_pga" | "golf_dp" | "golf_liv" | "golf_lpga":
            return _parse_golf("golf", scores, ts)
        case "horse_racing_us" | "horse_racing_uk" | "horse_racing_au" | "horse_racing_hk":
            return _parse_horse_racing(scores, ts)
        case "f1":
            return _parse_motorsport("formula1", scores, ts)
        case "motogp":
            return _parse_motorsport("motogp", scores, ts)
        case _:
            return []
