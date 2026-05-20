"""Goalserve inplay feed per-sport parsers。

每个 sport parser 把 Goalserve JSON events dict 转换成 LiveEvent 列表。
所有 parser 共用同一套 core flags → SportsLiveGameStatus 映射。

Goalserve inplay feed 状态通过 core flags 判定（非 time_status 整数）：
  removed="1"  → 跳过，不进结果
  finished="1" → ENDED
  stopped="1"  → PAUSED（节间休息）
  其余         → LIVE

Goalserve inplay feed 用 bet365 的赔率，每场含 14+ 盘口市场，
供策略层做 Polymarket 价格交叉验证。赔率放在 source_payload["goalserve_odds"]。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from polymarket_trader.domain.sports_live import (
    BaseballGameState,
    EsportsGameState,
    LiveEvent,
    LiveEventKind,
    Participant,
    SoccerGameState,
    SportsLiveGameStatus,
    TennisGameState,
    VolleyballGameState,
)
from polymarket_trader.infra.sports.common import utc_now


# ---------------------------------------------------------------------------
# Goalserve odds 数据结构（供策略定价用）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoalserveOutcome:
    """Goalserve 单个赔率结果。"""

    name: str
    value_eu: Decimal
    implied_prob: Decimal
    handicap: str
    suspended: bool


@dataclass(frozen=True, slots=True)
class GoalserveMarket:
    """Goalserve 单个盘口市场（Moneyline / Spread / Over-Under 等）。"""

    market_id: int
    name: str
    suspended: bool
    outcomes: tuple[GoalserveOutcome, ...]


@dataclass(frozen=True, slots=True)
class GoalserveOdds:
    """Goalserve inplay 实时赔率快照。"""

    event_id: str
    markets: tuple[GoalserveMarket, ...]

    def moneyline(self) -> GoalserveMarket | None:
        """返回 Moneyline 市场（优先匹配 Game Lines Money Line）。"""
        for mkt in self.markets:
            low = mkt.name.lower()
            if "money line" in low and "half" not in low and "quarter" not in low:
                return mkt
        return None

    def as_dict(self) -> dict[str, Any]:
        """转成可放入 source_payload 的 dict。"""
        return {
            "event_id": self.event_id,
            "markets": [
                {
                    "market_id": m.market_id,
                    "name": m.name,
                    "suspended": m.suspended,
                    "outcomes": [
                        {
                            "name": o.name,
                            "value_eu": str(o.value_eu),
                            "implied_prob": str(o.implied_prob),
                            "handicap": o.handicap,
                            "suspended": o.suspended,
                        }
                        for o in m.outcomes
                    ],
                }
                for m in self.markets
            ],
        }


# ---------------------------------------------------------------------------
# 核心工具
# ---------------------------------------------------------------------------


def _core_to_status(core: Mapping[str, Any]) -> SportsLiveGameStatus:
    if core.get("removed") == "1":
        return SportsLiveGameStatus.UNKNOWN  # 调用方应跳过
    if core.get("finished") == "1":
        return SportsLiveGameStatus.ENDED
    if core.get("stopped") == "1":
        return SportsLiveGameStatus.PAUSED
    return SportsLiveGameStatus.LIVE


def _int_val(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _dec_val(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError):
        return None


def _parse_start_time(ts_utc: Any) -> datetime | None:
    """把 Goalserve start_ts_utc（毫秒 epoch）转为 datetime。"""
    if not ts_utc:
        return None
    try:
        ms = int(str(ts_utc).strip())
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_seconds_remaining(seconds_str: Any) -> int | None:
    """把 'MM:SS' 格式转成总秒数。"""
    if not seconds_str:
        return None
    try:
        parts = str(seconds_str).strip().split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return None
    except (TypeError, ValueError):
        return None


def _stats_by_name(stats: Mapping[str, Any]) -> dict[str, Any]:
    """把 stats dict（键为 "0","1",...）重新按 name 索引。"""
    result: dict[str, Any] = {}
    for entry in stats.values():
        name = entry.get("name")
        if name:
            result[str(name)] = entry
    return result


def _parse_odds(odds_raw: Mapping[str, Any], event_id: str) -> GoalserveOdds:
    """解析 Goalserve odds dict → GoalserveOdds。"""
    markets: list[GoalserveMarket] = []
    for _mid, market_data in odds_raw.items():
        participants_raw = market_data.get("participants", {})
        outcomes: list[GoalserveOutcome] = []
        for outcome_data in participants_raw.values():
            eu_raw = outcome_data.get("value_eu")
            eu = _dec_val(eu_raw)
            if eu is None or eu <= 0:
                continue
            implied = Decimal("1") / eu
            outcomes.append(
                GoalserveOutcome(
                    name=str(outcome_data.get("name", "")),
                    value_eu=eu,
                    implied_prob=implied.quantize(Decimal("0.0001")),
                    handicap=str(outcome_data.get("handicap", "") or ""),
                    suspended=outcome_data.get("suspend") == "1",
                )
            )
        markets.append(
            GoalserveMarket(
                market_id=int(market_data.get("id", 0)),
                name=str(market_data.get("name", "")),
                suspended=market_data.get("suspend") == "1",
                outcomes=tuple(outcomes),
            )
        )
    return GoalserveOdds(event_id=event_id, markets=tuple(markets))


# ---------------------------------------------------------------------------
# Basketball
# ---------------------------------------------------------------------------


def _parse_basketball(events_dict: Mapping[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in events_dict.items():
        core = ev.get("core", {})
        if core.get("removed") == "1":
            continue
        status = _core_to_status(core)
        info = ev.get("info", {})
        team = ev.get("team_info", {})
        stats = ev.get("stats", {})
        by_name = _stats_by_name(stats)

        home_name = team.get("home", {}).get("name", "")
        away_name = team.get("away", {}).get("name", "")
        home_score = _int_val(team.get("home", {}).get("score"))
        away_score = _int_val(team.get("away", {}).get("score"))

        # 分节得分 via stats
        quarter_stats: dict[str, tuple[int | None, int | None]] = {}
        for key in ("1", "2", "Half", "3", "4", "OT"):
            entry = by_name.get(key)
            if entry:
                quarter_stats[key] = (_int_val(entry.get("home")), _int_val(entry.get("away")))

        odds = _parse_odds(ev.get("odds", {}), event_id)

        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=info.get("league", ""),
                sport="basketball",
                participants=(
                    Participant(
                        role="home",
                        name=home_name,
                        score=home_score,
                        external_ids={"goalserve": event_id},
                    ),
                    Participant(
                        role="away",
                        name=away_name,
                        score=away_score,
                        external_ids={"goalserve": event_id},
                    ),
                ),
                status=status,
                period=info.get("period", ""),
                seconds_remaining=_parse_seconds_remaining(info.get("seconds")),
                event_name=info.get("name", ""),
                event_start_time=_parse_start_time(info.get("start_ts_utc")),
                external_ids={"goalserve": event_id, "mid": info.get("mid", "")},
                raw_status=info.get("period"),
                observed_at=observed_at,
                source_payload={
                    "goalserve_odds": odds.as_dict(),
                    "quarter_stats": {k: {"home": h, "away": a} for k, (h, a) in quarter_stats.items()},
                },
            )
        )
    return results


# ---------------------------------------------------------------------------
# Soccer
# ---------------------------------------------------------------------------

_SOCCER_PERIOD_MAP: dict[str, str] = {
    "1st half": "first_half",
    "2nd half": "second_half",
    "first half": "first_half",
    "second half": "second_half",
    "extra time": "extra_time",
    "half time": "first_half",
    "halftime": "first_half",
    "break time": "first_half",
    "penalties": "penalties",
}


def _parse_soccer(events_dict: Mapping[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in events_dict.items():
        core = ev.get("core", {})
        if core.get("removed") == "1":
            continue
        status = _core_to_status(core)
        info = ev.get("info", {})
        team = ev.get("team_info", {})
        stats = ev.get("stats", {})
        by_name = _stats_by_name(stats)

        home_name = team.get("home", {}).get("name", "")
        away_name = team.get("away", {}).get("name", "")
        _igoal = by_name.get("IGoal") or {}
        home_score = _int_val(_igoal.get("home")) if _igoal.get("home") is not None else _int_val(team.get("home", {}).get("score"))
        away_score = _int_val(_igoal.get("away")) if _igoal.get("away") is not None else _int_val(team.get("away", {}).get("score"))

        period_raw = info.get("period", "")
        soccer_period = _SOCCER_PERIOD_MAP.get(period_raw.lower(), None)
        clock_minutes = _int_val(info.get("minute"))

        soccer_state = SoccerGameState(
            period=soccer_period,
            clock_minutes=clock_minutes,
            home_red_cards=_int_val(by_name.get("IRedCard", {}).get("home")) or 0,
            away_red_cards=_int_val(by_name.get("IRedCard", {}).get("away")) or 0,
            home_yellow_cards=_int_val(by_name.get("IYellowCard", {}).get("home")) or 0,
            away_yellow_cards=_int_val(by_name.get("IYellowCard", {}).get("away")) or 0,
        )
        odds = _parse_odds(ev.get("odds", {}), event_id)

        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=info.get("league", ""),
                sport="soccer",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=period_raw,
                seconds_remaining=None,
                event_name=info.get("name", ""),
                event_start_time=_parse_start_time(info.get("start_ts_utc")),
                external_ids={"goalserve": event_id, "mid": info.get("mid", "")},
                raw_status=period_raw,
                observed_at=observed_at,
                soccer_state=soccer_state,
                source_payload={"goalserve_odds": odds.as_dict()},
            )
        )
    return results


# ---------------------------------------------------------------------------
# Hockey
# ---------------------------------------------------------------------------


def _parse_hockey(events_dict: Mapping[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in events_dict.items():
        core = ev.get("core", {})
        if core.get("removed") == "1":
            continue
        status = _core_to_status(core)
        info = ev.get("info", {})
        team = ev.get("team_info", {})
        stats = ev.get("stats", {})
        by_name = _stats_by_name(stats)

        home_name = team.get("home", {}).get("name", "")
        away_name = team.get("away", {}).get("name", "")
        home_score = _int_val(team.get("home", {}).get("score"))
        away_score = _int_val(team.get("away", {}).get("score"))

        # Period scores: P1, P2, P3, T (total/overtime)
        period_stats: dict[str, tuple[int | None, int | None]] = {}
        for key in ("P1", "P2", "P3", "T"):
            entry = by_name.get(key)
            if entry:
                period_stats[key] = (_int_val(entry.get("home")), _int_val(entry.get("away")))

        odds = _parse_odds(ev.get("odds", {}), event_id)

        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=info.get("league", ""),
                sport="ice-hockey",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=info.get("period", ""),
                seconds_remaining=_parse_seconds_remaining(info.get("seconds")),
                event_name=info.get("name", ""),
                event_start_time=_parse_start_time(info.get("start_ts_utc")),
                external_ids={"goalserve": event_id, "mid": info.get("mid", "")},
                raw_status=info.get("period"),
                observed_at=observed_at,
                source_payload={
                    "goalserve_odds": odds.as_dict(),
                    "period_stats": {k: {"home": h, "away": a} for k, (h, a) in period_stats.items()},
                },
            )
        )
    return results


# ---------------------------------------------------------------------------
# Baseball
# ---------------------------------------------------------------------------


def _parse_baseball(events_dict: Mapping[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in events_dict.items():
        core = ev.get("core", {})
        if core.get("removed") == "1":
            continue
        status = _core_to_status(core)
        info = ev.get("info", {})
        team = ev.get("team_info", {})
        stats = ev.get("stats", {})
        by_name = _stats_by_name(stats)

        home_name = team.get("home", {}).get("name", "")
        away_name = team.get("away", {}).get("name", "")
        home_score = _int_val(team.get("home", {}).get("score"))
        away_score = _int_val(team.get("away", {}).get("score"))

        # 局分：stats name="1".."9"（可能还有 "10","11" 延长局）；不 break 以免遗漏不连续局
        inning_scores: list[tuple[int | None, int | None]] = []
        for i in range(1, 15):
            entry = by_name.get(str(i))
            if entry is not None:
                inning_scores.append((_int_val(entry.get("home")), _int_val(entry.get("away"))))

        # 解析 inning 和 half（如 "Inning 5 Top" / "Inning 5 Bottom" / "Inning 5"）
        period_raw = info.get("period", "")
        current_inning: int | None = None
        inning_half: str | None = None
        parts = period_raw.lower().split()
        if "inning" in parts:
            idx = parts.index("inning")
            if idx + 1 < len(parts):
                current_inning = _int_val(parts[idx + 1])
            if "top" in parts:
                inning_half = "top"
            elif "bottom" in parts or "bot" in parts:
                inning_half = "bottom"

        baseball_state = BaseballGameState(
            current_inning=current_inning,
            inning_half=inning_half,
        )
        odds = _parse_odds(ev.get("odds", {}), event_id)

        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=info.get("league", ""),
                sport="baseball",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=period_raw,
                seconds_remaining=None,
                event_name=info.get("name", ""),
                event_start_time=_parse_start_time(info.get("start_ts_utc")),
                external_ids={"goalserve": event_id, "mid": info.get("mid", "")},
                raw_status=period_raw,
                observed_at=observed_at,
                baseball_state=baseball_state,
                source_payload={
                    "goalserve_odds": odds.as_dict(),
                    "inning_scores": [{"home": h, "away": a} for h, a in inning_scores],
                },
            )
        )
    return results


# ---------------------------------------------------------------------------
# Tennis
# ---------------------------------------------------------------------------

def _parse_tennis(events_dict: Mapping[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in events_dict.items():
        core = ev.get("core", {})
        if core.get("removed") == "1":
            continue
        status = _core_to_status(core)
        info = ev.get("info", {})
        team = ev.get("team_info", {})
        stats = ev.get("stats", {})
        by_name = _stats_by_name(stats)

        home_name = team.get("home", {}).get("name", "")
        away_name = team.get("away", {}).get("name", "")

        # Set scores: S1, S2, S3, (S4, S5 for 5-set)
        set_scores: list[tuple[int, int]] = []
        for i in range(1, 6):
            entry = by_name.get(f"S{i}")
            if entry is None:
                break
            h = _int_val(entry.get("home"))
            a = _int_val(entry.get("away"))
            if h is not None and a is not None:
                set_scores.append((h, a))

        # Serving side: TURN home=1 → home serving
        turn_entry = by_name.get("TURN", {})
        home_turn = _int_val(turn_entry.get("home"))
        serving_side: str | None = None
        if home_turn == 1:
            serving_side = "home"
        elif home_turn == 0:
            serving_side = "away"

        # Game points: POINTS
        points_entry = by_name.get("POINTS", {})
        home_point = str(points_entry.get("home", "")) if points_entry else None
        away_point = str(points_entry.get("away", "")) if points_entry else None

        # Sets won
        home_sets = sum(1 for h, a in set_scores if h > a)
        away_sets = sum(1 for h, a in set_scores if a > h)

        # Current set games
        period_raw = info.get("period", "")
        current_set: int | None = None
        parts = period_raw.lower().split()
        if "set" in parts:
            idx = parts.index("set")
            if idx + 1 < len(parts):
                current_set = _int_val(parts[idx + 1])
        home_current = set_scores[-1][0] if set_scores else None
        away_current = set_scores[-1][1] if set_scores else None

        tennis_state = TennisGameState(
            home_sets_won=home_sets,
            away_sets_won=away_sets,
            current_set=current_set,
            home_current_set_games=home_current,
            away_current_set_games=away_current,
            set_scores=tuple(set_scores),
            home_point=home_point,
            away_point=away_point,
            serving_side=serving_side,
        )

        score_str = info.get("score", "")
        odds = _parse_odds(ev.get("odds", {}), event_id)

        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=info.get("league", ""),
                sport="tennis",
                participants=(
                    Participant(role="home", name=home_name, score=home_sets, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_sets, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=period_raw,
                seconds_remaining=None,
                event_name=info.get("name", ""),
                event_start_time=_parse_start_time(info.get("start_ts_utc")),
                external_ids={"goalserve": event_id, "mid": info.get("mid", "")},
                raw_status=period_raw,
                observed_at=observed_at,
                tennis_state=tennis_state,
                source_payload={"goalserve_odds": odds.as_dict(), "score_str": score_str},
            )
        )
    return results


# ---------------------------------------------------------------------------
# Esports
# ---------------------------------------------------------------------------


def _parse_esports(events_dict: Mapping[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in events_dict.items():
        core = ev.get("core", {})
        if core.get("removed") == "1":
            continue
        status = _core_to_status(core)
        info = ev.get("info", {})
        team = ev.get("team_info", {})

        home_name = team.get("home", {}).get("name", "")
        away_name = team.get("away", {}).get("name", "")
        home_score = _int_val(team.get("home", {}).get("score"))
        away_score = _int_val(team.get("away", {}).get("score"))

        esports_state = EsportsGameState(
            home_maps_won=home_score or 0,
            away_maps_won=away_score or 0,
        )
        odds = _parse_odds(ev.get("odds", {}), event_id)

        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=info.get("league", ""),
                sport="esports",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=info.get("period", ""),
                seconds_remaining=None,
                event_name=info.get("name", ""),
                event_start_time=_parse_start_time(info.get("start_ts_utc")),
                external_ids={"goalserve": event_id, "mid": info.get("mid", "")},
                raw_status=info.get("period"),
                observed_at=observed_at,
                esports_state=esports_state,
                source_payload={"goalserve_odds": odds.as_dict()},
            )
        )
    return results


# ---------------------------------------------------------------------------
# American Football
# ---------------------------------------------------------------------------


def _parse_amfootball(events_dict: Mapping[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """美式足球：字段结构与篮球相近（Q1-Q4, OT）。"""
    results: list[LiveEvent] = []
    for event_id, ev in events_dict.items():
        core = ev.get("core", {})
        if core.get("removed") == "1":
            continue
        status = _core_to_status(core)
        info = ev.get("info", {})
        team = ev.get("team_info", {})
        stats = ev.get("stats", {})
        by_name = _stats_by_name(stats)

        home_name = team.get("home", {}).get("name", "")
        away_name = team.get("away", {}).get("name", "")
        home_score = _int_val(team.get("home", {}).get("score"))
        away_score = _int_val(team.get("away", {}).get("score"))

        quarter_stats: dict[str, tuple[int | None, int | None]] = {}
        for key in ("1", "2", "Half", "3", "4", "OT"):
            entry = by_name.get(key)
            if entry:
                quarter_stats[key] = (_int_val(entry.get("home")), _int_val(entry.get("away")))

        odds = _parse_odds(ev.get("odds", {}), event_id)

        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=info.get("league", ""),
                sport="american-football",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=info.get("period", ""),
                seconds_remaining=_parse_seconds_remaining(info.get("seconds")),
                event_name=info.get("name", ""),
                event_start_time=_parse_start_time(info.get("start_ts_utc")),
                external_ids={"goalserve": event_id, "mid": info.get("mid", "")},
                raw_status=info.get("period"),
                observed_at=observed_at,
                source_payload={
                    "goalserve_odds": odds.as_dict(),
                    "quarter_stats": {k: {"home": h, "away": a} for k, (h, a) in quarter_stats.items()},
                },
            )
        )
    return results


# ---------------------------------------------------------------------------
# Volleyball
# ---------------------------------------------------------------------------


def _parse_volleyball(events_dict: Mapping[str, Any], observed_at: datetime) -> list[LiveEvent]:
    """排球：盘分结构与网球相近，stats 用 S1-S5 记录各盘比分。"""
    results: list[LiveEvent] = []
    for event_id, ev in events_dict.items():
        core = ev.get("core", {})
        if core.get("removed") == "1":
            continue
        status = _core_to_status(core)
        info = ev.get("info", {})
        team = ev.get("team_info", {})
        stats = ev.get("stats", {})
        by_name = _stats_by_name(stats)

        home_name = team.get("home", {}).get("name", "")
        away_name = team.get("away", {}).get("name", "")
        home_score = _int_val(team.get("home", {}).get("score"))
        away_score = _int_val(team.get("away", {}).get("score"))

        set_scores: list[tuple[int, int]] = []
        for key in ("S1", "S2", "S3", "S4", "S5"):
            entry = by_name.get(key)
            if entry:
                h = _int_val(entry.get("home"))
                a = _int_val(entry.get("away"))
                if h is not None and a is not None:
                    set_scores.append((h, a))

        # 当前盘号从 period 字段解析（"Set 1"–"Set 5"）
        period_str = info.get("period", "")
        current_set: int | None = None
        if period_str.lower().startswith("set "):
            try:
                current_set = int(period_str.split()[-1])
            except (ValueError, IndexError):
                pass

        # 当前盘即时分：取最后一条 set_score 对应的得分，或从 POINTS 读
        home_cur: int | None = None
        away_cur: int | None = None
        points_entry = by_name.get("POINTS")
        if points_entry:
            home_cur = _int_val(points_entry.get("home"))
            away_cur = _int_val(points_entry.get("away"))

        vball_state = VolleyballGameState(
            home_sets_won=home_score or 0,
            away_sets_won=away_score or 0,
            current_set=current_set,
            home_current_set_points=home_cur,
            away_current_set_points=away_cur,
            set_scores=tuple(set_scores),
        )
        odds = _parse_odds(ev.get("odds", {}), event_id)

        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=info.get("league", ""),
                sport="volleyball",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=period_str,
                seconds_remaining=None,
                event_name=info.get("name", ""),
                event_start_time=_parse_start_time(info.get("start_ts_utc")),
                external_ids={"goalserve": event_id, "mid": info.get("mid", "")},
                raw_status=period_str or None,
                observed_at=observed_at,
                volleyball_state=vball_state,
                source_payload={"goalserve_odds": odds.as_dict()},
            )
        )
    return results


# ---------------------------------------------------------------------------
# 顶层分派
# ---------------------------------------------------------------------------

_SPORT_PARSERS = {
    "basketball": _parse_basketball,
    "soccer": _parse_soccer,
    "hockey": _parse_hockey,
    "baseball": _parse_baseball,
    "tennis": _parse_tennis,
    "esports": _parse_esports,
    "amfootball": _parse_amfootball,
    "volleyball": _parse_volleyball,
}


def parse_goalserve_sport(
    sport: str,
    data: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
) -> list[LiveEvent]:
    """顶层分派：按 sport 调对应 parser，返回 LiveEvent 列表。

    data 为 Goalserve inplay JSON 根节点（含 "events" key）。
    removed="1" 的事件由各 parser 内部过滤。
    """
    ts = observed_at or utc_now()
    parser = _SPORT_PARSERS.get(sport.lower())
    if parser is None:
        return []
    events_dict = data.get("events", {})
    if not isinstance(events_dict, dict):
        return []
    return parser(events_dict, ts)
