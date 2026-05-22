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

import re
from datetime import datetime
from typing import Any

from polymarket_trader.domain.sports_live import (
    BaseballGameState,
    BasketballGameState,
    CricketGameState,
    EsportsGameState,
    HandballGameState,
    LiveEvent,
    LiveEventKind,
    MMAFightState,
    Participant,
    RaceState,
    RugbyGameState,
    SoccerGameState,
    SportsLiveGameStatus,
    TennisGameState,
    VolleyballGameState,
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
    # Period/set names and intermissions indicate an in-progress game
    if any(kw in s for kw in ("set ", "quarter", "period", "inning", "half", "break time", "intermission")):
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
#
# 实测 cricket/livescore 真实结构（与早期推断不同，已按真实 feed 校准）：
#   match.localteam / match.visitorteam: {id, name, totalscore: "208/6" 或 "158",
#       winner: "True"/"False"}
#   match.comment: {first_batting_teamId, toss_winner_teamId,
#       post: "Middlesex need 72 runs in 29 balls." / "Nepal won by 122 runs"}
#   match.inning[]: 每局 {inningnum: "1"/"2", team: "localteam"/"visitorteam",
#       total: {tot: "208 ( 20 )", wickets: "6"}}
#   match.type: "T20" / "ODI" / "TEST"
# totalscore 字符串 "runs/wickets"——全员出局或一局完成时只剩 "runs"（wickets=10
# 或局已收）。chase（追分）锁定的关键信号 required_runs / required_balls 直接来自
# comment.post，正则解析；first-innings target = 首打方得分 + 1。

# comment.post 形如 "Middlesex need 72 runs in 29 balls."
_CRICKET_CHASE_RE = re.compile(
    r"need\s+(\d+)\s+runs?\s+in\s+(\d+)\s+ball", re.IGNORECASE
)


def _parse_cricket_score(raw: Any) -> tuple[int | None, int | None]:
    """解析 totalscore 字符串 "208/6" → (runs, wickets)；"158" → (158, None)。

    wickets 缺省（无 "/" 部分）返回 None——可能是一局已收、或数据未给。
    """
    text = _str_val(raw)
    if not text:
        return None, None
    if "/" in text:
        runs_part, wkts_part = text.split("/", 1)
        return _int_val(runs_part), _int_val(wkts_part)
    return _int_val(text), None


def _parse_cricket(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    for match in _iter_matches(scores):
        event_id = _str_val(match.get("id")) or _str_val(match.get("matchid"))
        if not event_id:
            continue
        status = _text_status(match.get("status"))
        home_team = match.get("localteam") if isinstance(match.get("localteam"), dict) else {}
        away_team = match.get("visitorteam") if isinstance(match.get("visitorteam"), dict) else {}
        # awayteam 是早期接口字段名；真实 cricket/livescore 用 visitorteam，二者都兼容。
        if not away_team:
            away_team = match.get("awayteam") if isinstance(match.get("awayteam"), dict) else {}
        home_name = _str_val(home_team.get("name"))
        away_name = _str_val(away_team.get("name"))
        home_runs, home_wkts = _parse_cricket_score(home_team.get("totalscore"))
        away_runs, away_wkts = _parse_cricket_score(away_team.get("totalscore"))
        league = _str_val(match.get("competition") or match.get("league") or "")
        match_type = _str_val(match.get("type"))
        comment = match.get("comment") if isinstance(match.get("comment"), dict) else {}

        # 首打方（设定 target 的一方）由 first_batting_teamId 与队伍 id 匹配决定。
        first_bat_id = _str_val(comment.get("first_batting_teamId"))
        home_id = _str_val(home_team.get("id"))
        away_id = _str_val(away_team.get("id"))
        first_batting_side: str | None = None
        if first_bat_id and first_bat_id == home_id:
            first_batting_side = "home"
        elif first_bat_id and first_bat_id == away_id:
            first_batting_side = "away"

        # inning[] 给出每局的 inningnum / team / 累计 wickets。
        innings_raw = match.get("inning") or []
        if isinstance(innings_raw, dict):
            innings_raw = [innings_raw]
        innings = [x for x in innings_raw if isinstance(x, dict)]

        current_innings: int | None = None
        batting_side: str | None = None
        runs: int | None = None
        wickets: int | None = None
        overs_completed: int | None = None
        balls_in_over: int | None = None
        if innings:
            last = innings[-1]
            current_innings = _int_val(last.get("inningnum"))
            team_key = _str_val(last.get("team")).lower()
            if team_key == "localteam":
                batting_side = "home"
            elif team_key in ("visitorteam", "awayteam"):
                batting_side = "away"
            total = last.get("total") if isinstance(last.get("total"), dict) else {}
            wickets = _int_val(total.get("wickets"))
            # total.tot 形如 "137 ( 15.1 )" → runs=137, overs=15, balls=1
            tot_text = _str_val(total.get("tot"))
            tot_match = re.match(r"\s*(\d+)\s*\(\s*([\d.]+)\s*\)", tot_text)
            if tot_match:
                runs = _int_val(tot_match.group(1))
                overs_text = tot_match.group(2)
                if "." in overs_text:
                    o_parts = overs_text.split(".", 1)
                    overs_completed = _int_val(o_parts[0])
                    balls_in_over = _int_val(o_parts[1])
                else:
                    overs_completed = _int_val(overs_text)
                    balls_in_over = 0
        # inning 缺失时回退到 totalscore（batting_side 仍未知则保持 None）。
        if runs is None and batting_side == "home":
            runs, wickets = home_runs, home_wkts
        elif runs is None and batting_side == "away":
            runs, wickets = away_runs, away_wkts

        # 追分目标：首打方得分 + 1。首打方一局尚未结束时不构成 target——
        # current_innings>=2 才说明进入追分局。
        target: int | None = None
        if first_batting_side == "home" and home_runs is not None:
            target = home_runs + 1
        elif first_batting_side == "away" and away_runs is not None:
            target = away_runs + 1

        # required_runs / required_balls 来自 comment.post（"need N runs in M balls"）；
        # 这是追分局最权威的实时信号，整数化避免 overs 浮点。
        required_runs: int | None = None
        required_balls: int | None = None
        post_text = _str_val(comment.get("post"))
        chase_match = _CRICKET_CHASE_RE.search(post_text)
        if chase_match:
            required_runs = _int_val(chase_match.group(1))
            required_balls = _int_val(chase_match.group(2))

        cricket_state = CricketGameState(
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
        # winner 字段（Finished 时）决定 participant.score 的胜负方向——
        # 整场胜负盘是 single_game moneyline，ended-moneyline 用 score 大小判定胜者。
        # 用 runs 作为 participant.score 不可靠（追分方赢时分数可能更低且追平即胜）；
        # 故 score 用胜负标志：胜者 1、负者 0、未结束 None。
        home_winner = _str_val(home_team.get("winner")).lower() in ("true", "yes", "1")
        away_winner = _str_val(away_team.get("winner")).lower() in ("true", "yes", "1")
        if status == SportsLiveGameStatus.ENDED and (home_winner or away_winner):
            home_score: int | None = 1 if home_winner else 0
            away_score: int | None = 1 if away_winner else 0
        else:
            home_score = None
            away_score = None
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
                period=match_type,
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

        # 真实 handball/live feed 把赛段放在 status 字段（"1st Half"/"2nd Half"），
        # 不带独立 status_str——优先 status_str（早期接口），回退 status。
        period_raw = _str_val(
            match.get("status_str") or match.get("period") or match.get("status") or ""
        )
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


def _rugby_seconds_remaining(status_raw: str, timer_raw: Any) -> int | None:
    """估算橄榄球剩余秒数。全场 80 分钟（不含加时）。timer = 已过分钟数。

    上半场 0-40 分钟；下半场 40-80 分钟；半场休息时剩 40 分钟。
    """
    s = status_raw.strip().lower()
    if s in ("half time", "halftime", "ht"):
        return 40 * 60
    try:
        elapsed = int(float(str(timer_raw or "").strip()))
    except (ValueError, TypeError):
        return None
    if elapsed <= 0:
        return None
    return max(0, 80 - elapsed) * 60


def _parse_rugby(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    events: list[LiveEvent] = []
    for match in _iter_matches(scores):
        event_id = _str_val(match.get("id") or match.get("matchid") or "")
        if not event_id:
            continue
        status_raw = _str_val(match.get("status"))
        status = _text_status(status_raw)
        home_team = match.get("localteam") or {}
        away_team = match.get("awayteam") or {}
        home_name = _str_val(home_team.get("name"))
        away_name = _str_val(away_team.get("name"))
        home_score = _int_val(home_team.get("totalscore") or home_team.get("score"))
        away_score = _int_val(away_team.get("totalscore") or away_team.get("score"))
        home_p1 = _int_val(home_team.get("t1"))
        away_p1 = _int_val(away_team.get("t1"))

        # 赛段：真实 rugby/home feed 用 status 字段表示赛段（"2nd Half" 等），
        # 部分响应另带独立 status_str——优先后者，回退 status。
        period_source = _str_val(match.get("status_str")) or status_raw
        rugby_period = _HALF_PERIOD_MAP.get(period_source.lower())
        seconds_remaining = (
            _rugby_seconds_remaining(period_source, match.get("timer"))
            if status == SportsLiveGameStatus.LIVE
            else None
        )

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
                period=period_source,
                seconds_remaining=seconds_remaining,
                raw_status=status_raw,
                observed_at=observed_at,
                external_ids={"goalserve": event_id},
                rugby_state=rugby_state,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Esports
# ---------------------------------------------------------------------------

# esports getfeed 状态文字 → 归一状态。
# inplay WS 不在套餐内（403），livescore getfeed esports/home 是唯一可用源。
_ESPORTS_STATUS_MAP: dict[str, SportsLiveGameStatus] = {
    "not started": SportsLiveGameStatus.SCHEDULED,
    "started": SportsLiveGameStatus.LIVE,
    "finished": SportsLiveGameStatus.ENDED,
    # Awarded = 因对手退赛/违规判定胜负，结果同样已确定。
    "awarded": SportsLiveGameStatus.ENDED,
    "cancelled": SportsLiveGameStatus.CANCELLED,
    "canceled": SportsLiveGameStatus.CANCELLED,
    "postponed": SportsLiveGameStatus.POSTPONED,
}


def _esports_status(raw: Any) -> SportsLiveGameStatus:
    """esports getfeed 状态归一。未知状态归 UNKNOWN，不静默当 LIVE。"""
    return _ESPORTS_STATUS_MAP.get(str(raw or "").strip().lower(), SportsLiveGameStatus.UNKNOWN)


def _parse_best_of(round_raw: Any) -> int | None:
    """从 ``@round`` 解析 best-of 局数：'BO3'→3 / 'BO5'→5 / 'BO1'→1。

    无法解析（空 / 非 BOn 格式）返回 None——esports 评估器据此给可审计拒绝原因。
    """
    text = str(round_raw or "").strip().upper()
    match = re.search(r"BO\s*(\d+)", text)
    if match is None:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def _iter_esports_matches(scores: dict[str, Any]) -> list[dict[str, Any]]:
    """esports getfeed 的 match 直接挂在 ``scores.match`` 下（无 category 包装）。

    ``match`` 可能是单个 dict 或 list——两种形态都展开。
    """
    raw = scores.get("match") or []
    if isinstance(raw, dict):
        raw = [raw]
    return [m for m in raw if isinstance(m, dict)]


def _at(mapping: dict[str, Any], key: str) -> Any:
    """读取 Goalserve 字段，兼容 ``?json=1`` 保留的 ``@`` 属性前缀与裸 key。

    esports/home 的 ``?json=1`` 响应把 XML 属性序列化为 ``@status``/``@id`` 等，
    与已校准的 cricket/rugby 裸 key 不同——此 helper 两种形态都接受。
    """
    if key in mapping:
        return mapping[key]
    return mapping.get(f"@{key}")


def _parse_esports(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """解析 esports getfeed（CS2/Dota2/LoL/Valorant）。

    ``localteam.@score`` / ``awayteam.@score`` = 各队已赢局数（maps won），
    扫尾胜负判定据此与 best-of 阈值比较。score 同时作为 LiveEvent.participants
    的 score，使已结束比赛走通用 ended-moneyline 评估器。
    """
    events: list[LiveEvent] = []
    for match in _iter_esports_matches(scores):
        event_id = _str_val(_at(match, "id") or _at(match, "matchid") or "")
        if not event_id:
            continue
        status_raw = _str_val(_at(match, "status"))
        status = _esports_status(status_raw)
        home_team = match.get("localteam") or {}
        away_team = match.get("awayteam") or {}
        home_name = _str_val(_at(home_team, "name"))
        away_name = _str_val(_at(away_team, "name"))
        home_maps = _int_val(_at(home_team, "score")) or 0
        away_maps = _int_val(_at(away_team, "score")) or 0
        best_of = _parse_best_of(_at(match, "round"))

        esports_state = EsportsGameState(
            best_of=best_of,
            home_maps_won=home_maps,
            away_maps_won=away_maps,
        )
        league = _str_val(_at(match, "league") or _at(match, "competition") or "")
        events.append(
            LiveEvent(
                source="goalserve_livescore",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=league,
                sport="esports",
                participants=(
                    Participant(role="home", name=home_name, score=home_maps, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_maps, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=_str_val(_at(match, "round")),
                raw_status=status_raw,
                observed_at=observed_at,
                external_ids={"goalserve": event_id},
                esports_state=esports_state,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Boxing / MMA
# ---------------------------------------------------------------------------


def _fight_winner_scores(
    status: SportsLiveGameStatus, winner_side: str | None
) -> tuple[int | None, int | None]:
    """格斗（拳击/MMA）胜负 → participant.score。

    格斗无可靠盘中比分模型——只有打完后 winner 字段才能确定胜负。已结束且
    winner 已知时胜者 1、负者 0，使比赛走通用 ended-moneyline 评估器；未结束
    或 winner 未知一律返回 (None, None)，绝不臆造比分（CLAUDE.md §17）。
    """
    if status != SportsLiveGameStatus.ENDED or winner_side is None:
        return None, None
    if winner_side == "home":
        return 1, 0
    if winner_side == "away":
        return 0, 1
    return None, None


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
        # 拳击无盘中比分模型——胜负只在打完后由 winner 字段确定。把胜者
        # participant.score 置 1、负者 0，使已结束比赛走通用 ended-moneyline
        # 评估器锁定胜方；未结束（winner 未知）保持 None，不臆造比分。
        home_score, away_score = _fight_winner_scores(status, winner_side)
        events.append(
            LiveEvent(
                source="goalserve_livescore",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=league,
                sport="boxing",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
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
        # MMA 无盘中比分模型——同 boxing，胜者 score=1、负者 0，已结束比赛走
        # 通用 ended-moneyline 评估器；未结束保持 None。
        home_score, away_score = _fight_winner_scores(status, winner_side)
        events.append(
            LiveEvent(
                source="goalserve_livescore",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league="",
                sport="mma",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
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
    # Between-inning states (Goalserve uses "End Xth" = end of inning X, game still live)
    "end 1st", "end 2nd", "end 3rd", "end 4th", "end 5th",
    "end 6th", "end 7th", "end 8th", "end 9th", "end extra innings",
    "1st set", "2nd set", "3rd set", "4th set", "5th set",
    "1st half", "2nd half", "halftime",
    # Generic fallback — some Goalserve feeds return "In Progress" without a
    # specific period/quarter string; this catches those cases.
    "in progress", "inprogress", "live",
})
_XML_ENDED_STATUSES = frozenset({
    "finished", "final", "ft", "after over time", "after et", "after pen.",
    "aet", "pen", "aps",
})
# Soccer-specific finished statuses (extra time / penalty shootout endings)
_SOCCER_ENDED_STATUSES = frozenset({
    "ft", "aet", "pen", "finished", "final", "after over time", "after et", "after pen.", "aps",
})
_XML_SCHED_STATUSES = frozenset({"not started", "ns", ""})
# Intermission keywords — the game is ongoing but between periods/halves
_XML_INTERMISSION_KEYWORDS = ("break time", "half time", "halftime", "intermission", "interval", "ht")


def _xml_status(raw: Any) -> SportsLiveGameStatus:
    s = str(raw or "").strip().lower()
    if s in _XML_ENDED_STATUSES:
        return SportsLiveGameStatus.ENDED
    if s in _XML_SCHED_STATUSES:
        return SportsLiveGameStatus.SCHEDULED
    if s in _XML_LIVE_STATUSES or any(kw in s for kw in ("quarter", "period", "inning", "set", "bottom", "top")):
        return SportsLiveGameStatus.LIVE
    # Hockey intermissions ("Break Time"), soccer half-time, etc.
    if any(kw in s for kw in _XML_INTERMISSION_KEYWORDS):
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


def _hockey_seconds_remaining(status: str, timer_raw: Any, periods_played: int = 0) -> int | None:
    """NHL/IIHF 剩余秒数估算（timer = 已打分钟数）。

    3×20 min 正常时间；加时赛 5 min (NHL) / 20 min (IIHF)。
    periods_played: 当 status 为 "Break Time" 时从外部传入已完成局数。
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
    # 中场休息：根据已完成局数估算剩余时间
    if "break time" in s or "intermission" in s or "interval" in s:
        periods_left = max(0, 3 - periods_played)
        return periods_left * period_minutes * 60
    return None


# ---------------------------------------------------------------------------
# Tennis player name helpers
# ---------------------------------------------------------------------------

def _tennis_surname(name: str) -> str | None:
    """提取网球选手姓氏供 Participant.short_name 用于市场文本匹配。

    Goalserve 格式多样：
      "Djokovic N."  → "Djokovic"
      "N. Djokovic"  → "Djokovic"
      "Novak Djokovic" → "Djokovic"
    规则：去掉首字母缩写词（单字符 + 可选点号），取剩余部分最后一词。
    """
    parts = [p.rstrip(".") for p in name.strip().split()]
    meaningful = [p for p in parts if len(p) > 1]
    return meaningful[-1] if meaningful else (parts[-1] if parts else None)


# ---------------------------------------------------------------------------
# Soccer（足球 / soccernew/home）
# ---------------------------------------------------------------------------


def _soccer_status(raw: Any) -> SportsLiveGameStatus:
    """解析 soccer livescore 的 status 字段。

    Goalserve soccernew/home status 格式：
      "18:00" / "22:00"  → 未开始（scheduled time）
      整数字符串 "3", "45" → 已过分钟数（live）
      "45+2", "90+3"      → 补时（live）
      "HT"                → 上半场结束/中场休息（live，paused）
      "FT"                → 比赛结束
      "AET", "Pen"        → 加时 / 点球后结束
      "?"                 → 未知（scheduled but no kick-off time data）
    """
    s = str(raw or "").strip()
    s_lower = s.lower()
    if s_lower in _SOCCER_ENDED_STATUSES:
        return SportsLiveGameStatus.ENDED
    # Scheduled time pattern: "HH:MM" or "?" or empty
    if re.fullmatch(r"\d{1,2}:\d{2}", s) or s == "?":
        return SportsLiveGameStatus.SCHEDULED
    # Halftime (paused between halves) — treated as LIVE for tail purposes
    if s_lower in ("ht", "half time", "halftime"):
        return SportsLiveGameStatus.LIVE
    # Numeric minute (possibly with injury time suffix like "45+2")
    if re.fullmatch(r"\d+(\+\d+)?", s):
        return SportsLiveGameStatus.LIVE
    # Extra time period labels
    if s_lower in ("et", "extra time", "aet pending", "pen pending"):
        return SportsLiveGameStatus.LIVE
    if not s:
        return SportsLiveGameStatus.SCHEDULED
    return SportsLiveGameStatus.UNKNOWN


def _soccer_seconds_remaining(status_raw: str, timer_raw: Any) -> int | None:
    """估算足球剩余秒数。

    全场90分钟（不含加时）。timer 字段 = 已过分钟数。
    上半场：0-45分钟；下半场：45-90分钟。
    """
    s = status_raw.strip()
    # Halftime: 45 minutes remaining in second half
    if s.lower() in ("ht", "half time", "halftime"):
        return 45 * 60
    # Extract elapsed minutes from timer or status
    try:
        elapsed = int(float(str(timer_raw or "").strip()))
    except (ValueError, TypeError):
        m = re.match(r"(\d+)", s)
        elapsed = int(m.group(1)) if m else 0
    if elapsed <= 0:
        return None
    remaining = max(0, 90 - elapsed) * 60
    return remaining


def _soccer_halftime_scores(match: dict[str, Any]) -> tuple[int | None, int | None]:
    """从 <ht score="[H - A]"/> 提取半场比分；未到半场该字段为空 → (None, None)。"""
    ht = match.get("ht")
    if not isinstance(ht, dict):
        return None, None
    text = str(ht.get("score") or "").strip()
    m = re.match(r"\[?\s*(\d+)\s*-\s*(\d+)\s*\]?", text)
    if m is None:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _parse_soccer_with_cats(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """解析 soccernew/home 的 category → match 结构。

    每个 category 的 name 作为 league。localteam / visitorteam 分别为主客场。
    goals 字段在未开始时为 "?"。
    """
    events: list[LiveEvent] = []
    categories = scores.get("category") or []
    if isinstance(categories, dict):
        categories = [categories]
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        cat_name = _str_val(cat.get("name"))
        raw_matches = cat.get("match") or []
        if isinstance(raw_matches, dict):
            raw_matches = [raw_matches]
        for match in raw_matches:
            if not isinstance(match, dict):
                continue
            event_id = _str_val(match.get("id") or match.get("static_id") or "")
            if not event_id:
                continue
            status_raw = _str_val(match.get("status"))
            status = _soccer_status(status_raw)
            home_team = match.get("localteam") or {}
            away_team = match.get("visitorteam") or {}
            home_name = _str_val(home_team.get("name"))
            away_name = _str_val(away_team.get("name"))
            if not home_name or not away_name:
                continue
            goals_home_raw = home_team.get("goals")
            goals_away_raw = away_team.get("goals")
            home_score = _int_val(goals_home_raw)
            away_score = _int_val(goals_away_raw)
            home_loc, home_nick = _split_team_name(home_name)
            away_loc, away_nick = _split_team_name(away_name)
            timer_raw = match.get("timer")
            seconds_remaining = _soccer_seconds_remaining(status_raw, timer_raw) if status == SportsLiveGameStatus.LIVE else None
            ht_home, ht_away = _soccer_halftime_scores(match)
            soccer_state = (
                SoccerGameState(home_halftime_score=ht_home, away_halftime_score=ht_away)
                if ht_home is not None and ht_away is not None
                else None
            )
            events.append(
                LiveEvent(
                    source="goalserve_livescore",
                    source_event_id=event_id,
                    kind=LiveEventKind.TEAM_MATCH,
                    league=cat_name,
                    sport="soccer",
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
                    soccer_state=soccer_state,
                )
            )
    return events


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


def _basketball_quarter_scores(team: dict[str, Any]) -> tuple[int | None, ...]:
    """从 hometeam/awayteam 的 q1..q4 属性提取各节得分；空字符串 → None。"""
    out: list[int | None] = []
    for q in ("q1", "q2", "q3", "q4"):
        raw = team.get(q)
        text = "" if raw is None else str(raw).strip()
        out.append(int(text) if text.lstrip("-").isdigit() else None)
    return tuple(out)


def _basketball_current_period(status_raw: str) -> int | None:
    """从 status 文本判定当前节。Halftime 映射为 3（上半场已结束）。"""
    s = status_raw.lower()
    if "halftime" in s or "half time" in s:
        return 3
    if "overtime" in s or "over time" in s or f" {s} ".find(" ot ") >= 0:
        return 5
    markers = (
        (1, ("1st", "q1", "quarter 1", "1 quarter")),
        (2, ("2nd", "q2", "quarter 2", "2 quarter")),
        (3, ("3rd", "q3", "quarter 3", "3 quarter")),
        (4, ("4th", "q4", "quarter 4", "4 quarter")),
    )
    for period, tokens in markers:
        if any(t in s for t in tokens):
            return period
    return None


def _basketball_state_from_match(match: dict[str, Any]) -> BasketballGameState:
    """从 XML 转换后的 dict 提取 BasketballGameState（分节比分）。"""
    home_team = match.get("localteam") or match.get("hometeam") or {}
    away_team = match.get("awayteam") or {}
    return BasketballGameState(
        current_period=_basketball_current_period(_str_val(match.get("status"))),
        home_quarter_scores=_basketball_quarter_scores(home_team if isinstance(home_team, dict) else {}),
        away_quarter_scores=_basketball_quarter_scores(away_team if isinstance(away_team, dict) else {}),
    )


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
            basketball_state = _basketball_state_from_match(match) if status == SportsLiveGameStatus.LIVE else None
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
                    basketball_state=basketball_state,
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
            # Count completed periods from period score data (_children from XML conversion)
            events_node = match.get("events") or {}
            period_children = events_node.get("_children") or []
            period_tags = {"firstperiod", "secondperiod", "thirdperiod"}
            periods_played = sum(
                1 for c in period_children
                if c.get("_tag") in period_tags and c.get("score", "") not in ("", None, " - ")
            )
            seconds_remaining = _hockey_seconds_remaining(status_raw, timer_raw, periods_played) if status == SportsLiveGameStatus.LIVE else None
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


def _baseball_inning_runs(team: dict[str, Any]) -> tuple[int | None, ...]:
    """从 localteam/awayteam 的 in1..in9 属性提取各局得分。

    空字符串（该局未开始）→ None。供分局盘口（NRFI 等）判定。
    """
    runs: list[int | None] = []
    for i in range(1, 10):
        raw = team.get(f"in{i}")
        text = "" if raw is None else str(raw).strip()
        runs.append(int(text) if text.lstrip("-").isdigit() else None)
    return tuple(runs)


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
    home_team = match.get("localteam") or match.get("hometeam") or {}
    away_team = match.get("awayteam") or {}
    return BaseballGameState(
        current_inning=inning,
        inning_half=half,
        home_inning_runs=_baseball_inning_runs(home_team if isinstance(home_team, dict) else {}),
        away_inning_runs=_baseball_inning_runs(away_team if isinstance(away_team, dict) else {}),
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
    # current_set 判定：s{i} 字段在某盘"进行中"就有值，不只是打完的盘。
    # 因此不能简单用 len(set_scores)+1（首盘进行中 5-2 会被算成第 2 盘）。
    # 用 totalscore（已赢盘数之和 = 已完成盘数）与已开始盘数对比：
    #   已完成 < 已开始 → 最后一盘进行中 → current = 已开始盘数；
    #   已完成 >= 已开始 → 该开始的盘都打完了 → current = 已开始盘数 + 1。
    started_sets = len(set_scores)
    completed_sets = home_sets + away_sets
    if started_sets == 0:
        current_set = 1
    elif completed_sets >= started_sets:
        current_set = started_sets + 1
    else:
        current_set = started_sets
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
                        Participant(role="home", name=home_name, score=home_sets, short_name=_tennis_surname(home_name), external_ids={"goalserve": event_id}),
                        Participant(role="away", name=away_name, score=away_sets, short_name=_tennis_surname(away_name), external_ids={"goalserve": event_id}),
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
# Volleyball（volleyball/home）
# ---------------------------------------------------------------------------


def _volleyball_state_from_match(match: dict[str, Any]) -> VolleyballGameState | None:
    """从 volleyball/home 的 localteam/awayteam 提取 VolleyballGameState。

    实测字段：totalscore=已赢盘数，s1..s5=各盘得分。未开始盘 s{i} 为空字符串。
    current_set 用「已开始盘数」推断：已完成盘数（totalscore 之和）< 已开始盘数
    → 最后一盘进行中；否则下一盘待开。与 tennis parser 同构。
    """
    home_team = match.get("localteam") or {}
    away_team = match.get("awayteam") or {}
    home_sets = _int_val(home_team.get("totalscore")) or 0
    away_sets = _int_val(away_team.get("totalscore")) or 0
    set_scores: list[tuple[int, int]] = []
    for i in range(1, 6):
        hs = _int_val(home_team.get(f"s{i}"))
        as_ = _int_val(away_team.get(f"s{i}"))
        if hs is None and as_ is None:
            break
        set_scores.append((hs or 0, as_ or 0))
    started_sets = len(set_scores)
    completed_sets = home_sets + away_sets
    if started_sets == 0:
        current_set: int | None = 1
    elif completed_sets >= started_sets:
        current_set = started_sets + 1
    else:
        current_set = started_sets
    home_cur: int | None = None
    away_cur: int | None = None
    if current_set is not None and 1 <= current_set <= started_sets:
        home_cur, away_cur = set_scores[current_set - 1]
    return VolleyballGameState(
        home_sets_won=home_sets,
        away_sets_won=away_sets,
        current_set=current_set if (current_set is not None and current_set <= 5) else None,
        home_current_set_points=home_cur,
        away_current_set_points=away_cur,
        set_scores=tuple(set_scores),
    )


def _parse_volleyball(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """解析 volleyball/home getfeed。

    结构：scores.category[].match[]，category.name 作为 league。
    status 文本 "Not Started"/"Set N"/"Finished" 由 _text_status 归一
    （"Set N" 命中 "set " 关键词 → LIVE）。LiveEvent.score 用已赢盘数。
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
            home_team = match.get("localteam") or {}
            away_team = match.get("awayteam") or {}
            home_name = _str_val(home_team.get("name"))
            away_name = _str_val(away_team.get("name"))
            if not home_name or not away_name:
                continue
            vb_state = (
                _volleyball_state_from_match(match)
                if status == SportsLiveGameStatus.LIVE
                else None
            )
            home_sets = _int_val(home_team.get("totalscore"))
            away_sets = _int_val(away_team.get("totalscore"))
            events.append(
                LiveEvent(
                    source="goalserve_livescore",
                    source_event_id=event_id,
                    kind=LiveEventKind.TEAM_MATCH,
                    league=cat_name,
                    sport="volleyball",
                    participants=(
                        Participant(role="home", name=home_name, score=home_sets, external_ids={"goalserve": event_id}),
                        Participant(role="away", name=away_name, score=away_sets, external_ids={"goalserve": event_id}),
                    ),
                    status=status,
                    period=status_raw,
                    raw_status=status_raw,
                    observed_at=observed_at,
                    external_ids={"goalserve": event_id},
                    volleyball_state=vb_state,
                )
            )
    return events


# ---------------------------------------------------------------------------
# American Football（football/home — Goalserve "football" = 美式橄榄球）
# ---------------------------------------------------------------------------


def _parse_amfootball(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """解析 football/home getfeed（CFL/UFL/AF1 等美式橄榄球联赛）。

    结构：scores.category[].match（单场为 dict，多场为 list），category.name 作 league。
    localteam/awayteam.totalscore = 全场总分；status "Not Started"/"1st Quarter"/
    "Finished" 由 _text_status 归一（"quarter" 命中 → LIVE）。
    无 domain AmFootballGameState——只产出基础 LiveEvent（score+period+status）；
    events.{firstquarter..overtime}.score 是分节比分，本兜底源暂不建分节模型。
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
            home_team = match.get("localteam") or {}
            away_team = match.get("awayteam") or {}
            home_name = _str_val(home_team.get("name"))
            away_name = _str_val(away_team.get("name"))
            if not home_name or not away_name:
                continue
            home_score = _int_val(home_team.get("totalscore"))
            away_score = _int_val(away_team.get("totalscore"))
            events.append(
                LiveEvent(
                    source="goalserve_livescore",
                    source_event_id=event_id,
                    kind=LiveEventKind.TEAM_MATCH,
                    league=cat_name,
                    sport="american-football",
                    participants=(
                        Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                        Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                    ),
                    status=status,
                    period=status_raw,
                    raw_status=status_raw,
                    observed_at=observed_at,
                    external_ids={"goalserve": event_id},
                )
            )
    return events


# ---------------------------------------------------------------------------
# Table Tennis（tennis_scores/tt_live）
# ---------------------------------------------------------------------------


def _parse_table_tennis(scores: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """解析 tennis_scores/tt_live getfeed（乒乓球 / WTT 等）。

    tt_live 与常规网球 tennis_scores/home 同属 tennis_scores 家族、同为 XML：
      <scores><category name="..."><match id=".." status=".."><player .../></match>
    _xml_to_livescore_dict 把两个 <player> 子节点转为 _player_list。
    实测 tennis_scores 网球 player 字段：name / totalscore / s1..s5 / winner /
    game_score / serve（见 d-1 历史 feed 的 <player> 行）；tt_live 当前无 live
    比赛，按家族同构假设 player 字段一致——首次拿到真实乒乓数据后需校验。

    乒乓球是「局制」（best-of-5/7 games，每局 11 分），与网球的「盘+局」、排球的
    「盘」语义都不同；domain 无对应 GameState，按 §任务约定只产出基础 LiveEvent
    （score=已赢局数 + period + status），不误用 TennisGameState/VolleyballGameState。
    participants.score 用 totalscore（已赢局数），使已结束比赛走通用 ended-moneyline
    评估器。set/game 关键词触发 _text_status → LIVE。
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
            players: list[dict[str, Any]] = match.get("_player_list") or []
            if len(players) < 2:
                continue
            home_name = _str_val(players[0].get("name"))
            away_name = _str_val(players[1].get("name"))
            if not home_name or not away_name:
                continue
            # totalscore = 已赢局数（games won）；缺省 0 而非 None，保证比分可比
            home_games = _int_val(players[0].get("totalscore"))
            away_games = _int_val(players[1].get("totalscore"))
            events.append(
                LiveEvent(
                    source="goalserve_livescore",
                    source_event_id=event_id,
                    kind=LiveEventKind.TEAM_MATCH,
                    league=cat_name,
                    sport="table-tennis",
                    participants=(
                        Participant(
                            role="home",
                            name=home_name,
                            score=home_games,
                            short_name=_tennis_surname(home_name),
                            external_ids={"goalserve": event_id},
                        ),
                        Participant(
                            role="away",
                            name=away_name,
                            score=away_games,
                            short_name=_tennis_surname(away_name),
                            external_ids={"goalserve": event_id},
                        ),
                    ),
                    status=status,
                    period=status_raw,
                    raw_status=status_raw,
                    observed_at=observed_at,
                    external_ids={"goalserve": event_id},
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
        case "soccer":
            return _parse_soccer_with_cats(scores, ts)
        case "basketball" | "nba" | "wnba":
            return _parse_basketball_with_cats(scores, ts)
        case "hockey" | "nhl":
            return _parse_hockey_with_cats(scores, ts)
        case "baseball" | "mlb":
            return _parse_baseball_with_cats(scores, ts)
        case "tennis":
            return _parse_tennis_with_cats(scores, ts)
        case "table-tennis":
            return _parse_table_tennis(scores, ts)
        case "cricket":
            return _parse_cricket(scores, ts)
        case "esports":
            return _parse_esports(scores, ts)
        case "handball":
            return _parse_handball(scores, ts)
        case "rugby":
            return _parse_rugby(scores, ts)
        case "volleyball":
            return _parse_volleyball(scores, ts)
        case "amfootball":
            return _parse_amfootball(scores, ts)
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
