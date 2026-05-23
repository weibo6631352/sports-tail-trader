"""Goalserve inplay HTTP-GZIP feed per-sport parsers。

数据源：``http://inplay.goalserve.com/inplay-{sport}.gz``——keyless（IP 白名单），
gunzip 后为 JSON，服务端每 1 秒刷新一次。feed 是完整快照而非增量推送。

feed 顶层结构：``{bm, updated, updated_ts, events: {match_id: EVENT}}``。
每个 EVENT 含 ``core / info / team_info / bonus / stream / sts / stats / extra /
history / odds``，本 parser 只解释交易需要的字段：

- ``core``  : 状态 flag——``finished`` / ``removed`` / ``stopped`` / ``blocked`` / ``nc``。
- ``info``  : ``id`` / ``name`` "A vs B" / ``sport`` / ``league`` / ``period`` /
              ``score`` "79:67"（部分运动）/ ``minute`` / ``state`` / ``start_ts_utc``。
- ``team_info`` : ``home/away`` 各 ``{name, score}``。
- ``stats`` : dict ``{index: {name, home, away}}``——逐运动的统计行。
              篮球有分节行（name "1".."4"/"OT"/"T"/"Half"）；网球/排球有 "S1".."S5"/"T"；
              棒球有逐局行 "1".."15"/"R"/"H"；电竞有 "Res"=已赢局数。
- ``odds``  : dict ``{market_id: {id, name, suspend, participants: {pid: {name,
              value_eu, handicap, suspend}}}}``——``value_eu`` = 欧洲十进制赔率，
              隐含概率 = 1 / value_eu。

输出 ``LiveEvent``（source="goalserve_inplay"），odds 归一到
``source_payload["goalserve_odds"]``，shape 与 GoalserveOdds.as_dict() 完全一致，
使下游 ``_extract_goalserve_*`` 无需任何改动即可消费。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from polymarket_trader.domain.sports_live import (
    BaseballGameState,
    BasketballGameState,
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

logger = logging.getLogger(__name__)

SOURCE = "goalserve_inplay"


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

    def as_dict(self) -> dict[str, Any]:
        """转成可放入 source_payload 的 dict（下游 _extract_goalserve_* 消费此 shape）。"""
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

# inplay feed 的 sport 路径 token → 内部规范 sport code。
# 路径 token 是 URL 里的固定写法（basket 而非 basketball），与 LiveEvent.sport
# 用的规范码（与 WS parser 对齐：basketball / ice-hockey / american-football）不同。
_SPORT_PATH_TO_CODE: dict[str, str] = {
    "soccer": "soccer",
    "basket": "basketball",
    "tennis": "tennis",
    "volleyball": "volleyball",
    "amfootball": "american-football",
    "esports": "esports",
    "hockey": "ice-hockey",
    "baseball": "baseball",
}


# ---------------------------------------------------------------------------
# 基础字段工具
# ---------------------------------------------------------------------------


def _int_val(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        # 部分字段是 "06:24" 这类带冒号的——取冒号前段。
        try:
            return int(text.split(":")[0])
        except (TypeError, ValueError):
            return None


def _dec_val(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, TypeError):
        return None


def _str_val(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _flag(value: Any) -> bool:
    """core flag 实测为 "1" / "0" / ""——只有 "1" 视为 True。"""
    return str(value).strip() == "1"


def _parse_start_time(info: dict[str, Any]) -> datetime | None:
    """从 info.start_ts_utc（epoch 秒）解析开赛时间；缺失或非法返回 None。"""
    raw = info.get("start_ts_utc") or info.get("start_ts")
    ts = _int_val(raw)
    if ts is None or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 状态映射
# ---------------------------------------------------------------------------

# info.period 文本里表示比赛已结束的关键词（小写匹配）。
_ENDED_PERIOD_KEYWORDS = (
    "finished",
    "final",
    "ended",
    "after over time",
    "after et",
    "after pen",
    "aet",
    "full time",
    "game over",
)
# info.period 文本里表示尚未开赛的关键词。
_SCHEDULED_PERIOD_KEYWORDS = ("not started", "scheduled")


def _map_status(core: dict[str, Any], info: dict[str, Any]) -> SportsLiveGameStatus:
    """把 core flag + info.period 映射到归一状态。

    判定优先级（core flag 是权威终态，period 文本仅在 flag 不决断时补充）：
      1. core.finished == "1"  → ENDED
      2. core.removed  == "1"  → CANCELLED（赛事被移除，等同取消）
      3. info.period 含 finished/final 等  → ENDED
      4. info.period 含 not started        → SCHEDULED
      5. core.stopped == "1"               → PAUSED（暂停/中断，与 ENDED 区分）
      6. 其余                              → LIVE

    注意：能出现在 inplay feed 里的赛事默认就是"进行中"——这是 inplay 流的语义，
    因此兜底为 LIVE 而非 SCHEDULED（与 WS parser 的 stp=0→LIVE 同理）。
    blocked flag 只表示盘口暂时锁定，不代表比赛暂停，不参与状态判定。
    """
    if _flag(core.get("finished")):
        return SportsLiveGameStatus.ENDED
    if _flag(core.get("removed")):
        return SportsLiveGameStatus.CANCELLED
    period = _str_val(info.get("period")).lower()
    if period:
        if any(kw in period for kw in _ENDED_PERIOD_KEYWORDS):
            return SportsLiveGameStatus.ENDED
        if any(kw in period for kw in _SCHEDULED_PERIOD_KEYWORDS):
            return SportsLiveGameStatus.SCHEDULED
    if _flag(core.get("stopped")):
        return SportsLiveGameStatus.PAUSED
    return SportsLiveGameStatus.LIVE


# ---------------------------------------------------------------------------
# stats 工具
# ---------------------------------------------------------------------------


def _stats_rows(event: dict[str, Any]) -> list[dict[str, Any]]:
    """把 stats dict ``{index: {name, home, away}}`` 展开成有序 row 列表。

    index 是字符串数字 key——按数值排序保证逐节/逐局顺序稳定。
    """
    stats = event.get("stats")
    if not isinstance(stats, dict):
        return []
    rows: list[tuple[int, dict[str, Any]]] = []
    for key, row in stats.items():
        if not isinstance(row, dict):
            continue
        try:
            idx = int(str(key))
        except (TypeError, ValueError):
            idx = 1_000_000
        rows.append((idx, row))
    rows.sort(key=lambda x: x[0])
    return [r for _idx, r in rows]


def _stat_row_by_name(event: dict[str, Any], name: str) -> dict[str, Any] | None:
    """按 stats row 的 ``name`` 字段查行（大小写不敏感）。"""
    target = name.strip().lower()
    for row in _stats_rows(event):
        if _str_val(row.get("name")).lower() == target:
            return row
    return None


def _team_score(team_info: dict[str, Any], side: str) -> int | None:
    """从 team_info.{home,away}.score 取整数比分；空字符串/None → None。"""
    team = team_info.get(side)
    if not isinstance(team, dict):
        return None
    return _int_val(team.get("score"))


def _score_from_info(info: dict[str, Any]) -> tuple[int | None, int | None]:
    """从 info.score "79:67" 解析 (home, away)；格式不符返回 (None, None)。"""
    raw = _str_val(info.get("score"))
    m = re.match(r"^\s*(\d+)\s*:\s*(\d+)\s*$", raw)
    if m is None:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _team_names(info: dict[str, Any], team_info: dict[str, Any]) -> tuple[str, str]:
    """优先 team_info 的队名；缺失时从 info.name "A vs B" 兜底拆分。"""
    home = _str_val((team_info.get("home") or {}).get("name"))
    away = _str_val((team_info.get("away") or {}).get("name"))
    if home and away:
        return home, away
    name = _str_val(info.get("name"))
    if " vs " in name:
        left, _, right = name.partition(" vs ")
        return home or left.strip(), away or right.strip()
    return home, away


# ---------------------------------------------------------------------------
# odds 解析
# ---------------------------------------------------------------------------

# 各运动「全场主胜负盘口」的原始 name → 归一成下游 _extract_goalserve_moneyline
# 能识别的 "money line"。inplay feed 各运动的全场胜负盘口命名不统一：
#   篮球 = "Game Lines Money Line"（已含 money line，原样可识别）
#   网球 = "To Win"
#   棒球/排球/电竞 = "Home/Away"
#   足球 = 无全场盘，只有 "1x2 (1st Half)" 等分段盘
# 下游靠 market name 含 "money line" 识别，因此对未含该串的全场盘做名称归一，
# 同时保留原始名到 original_name，避免下游误判语义。
_FULLGAME_MONEYLINE_NAMES: dict[str, frozenset[str]] = {
    "tennis": frozenset({"to win", "match winner"}),
    "baseball": frozenset({"home/away"}),
    "volleyball": frozenset({"home/away"}),
    "esports": frozenset({"home/away"}),
    "basketball": frozenset({"home/away"}),
    "ice-hockey": frozenset({"home/away"}),
    "american-football": frozenset({"home/away"}),
}

# Goalserve participant.name 的胜负方写法 → 归一 outcome 名。
# 下游 _extract_goalserve_moneyline 认 "home"/"1" 与 "away"/"2"，
# inplay feed 用 "Home"/"Away"，归一为小写直接兼容。
_OUTCOME_NAME_NORMALIZE: dict[str, str] = {
    "home": "Home",
    "away": "Away",
    "draw": "Draw",
    "1": "Home",
    "2": "Away",
    "x": "Draw",
}


def _normalize_market_name(sport_code: str, raw_name: str) -> str:
    """把全场胜负盘口名归一到下游可识别的 "Money Line"。

    只对「全场」胜负盘做归一——分段盘（含 1st Half / Quarter / Set 等）不动，
    避免下游把分段盘当全场 moneyline 误用（语义不同）。
    """
    low = raw_name.strip().lower()
    if not low:
        return raw_name
    # 分段盘不归一：下游 _extract_goalserve_* 自己按全场语义过滤。
    if any(seg in low for seg in ("half", "quarter", "set", "inning", "period", "minute", "map")):
        return raw_name
    aliases = _FULLGAME_MONEYLINE_NAMES.get(sport_code, frozenset())
    if low in aliases:
        return "Money Line"
    return raw_name


# 已告警过的「疑似但未识别」盘口名——模块级 set 去重，避免每秒轮询刷屏。
# key = (sport_code, lower(name))；进程生命周期内每个新名字只 warn 一次。
_unrecognized_odds_market_names_seen: set[tuple[str, str]] = set()

# 启发式：盘口名里出现这些词 → 看起来「应该」是全场 moneyline/totals/spread。
# 命中下游可识别串（money line / over/under handicap 等）的不算未识别。
_LOOKS_LIKE_MARKET_KEYWORDS = (
    "money",
    "line",
    "winner",
    "to win",
    "total",
    "handicap",
    "spread",
    "over/under",
    "over / under",
)
# 下游 _extract_goalserve_* 已能识别的盘口名串，或本 parser 会归一成可识别名的串。
_RECOGNIZED_MARKET_SUBSTRINGS = (
    "money line",
    "handicap",
    "over/under",
    "over / under",
)


def _warn_if_unrecognized_market(sport_code: str, raw_name: str, normalized_name: str) -> None:
    """疑似全场 moneyline/totals/spread 但未匹配任何已知 pattern 时告警一次。

    只做观测——不改任何匹配行为。Goalserve 重命名盘口或新运动用了不同命名时，
    odds 会被下游静默丢弃；这里按 (sport, name) 去重 warn，使其可被发现排查。
    廉价：parser 每秒/运动跑一次，命中去重 set 后是 O(1) 提前返回。
    """
    low = raw_name.strip().lower()
    if not low:
        return
    # 分段盘不在「全场盘口」观测范围内——下游本就按全场语义过滤分段盘。
    if any(seg in low for seg in ("half", "quarter", "set", "inning", "period", "minute", "map")):
        return
    if not any(kw in low for kw in _LOOKS_LIKE_MARKET_KEYWORDS):
        return
    # 已归一成 Money Line，或本身含下游可识别串 → 不算未识别。
    norm_low = normalized_name.strip().lower()
    if norm_low == "money line" or any(s in norm_low for s in _RECOGNIZED_MARKET_SUBSTRINGS):
        return
    key = (sport_code, low)
    if key in _unrecognized_odds_market_names_seen:
        return
    _unrecognized_odds_market_names_seen.add(key)
    logger.warning(
        "goalserve_inplay: unrecognized odds market name (sport=%s, name=%r) — "
        "looks like a full-game moneyline/totals/spread but matched no known pattern; "
        "Goalserve rename or new sport — odds silently dropped downstream",
        sport_code,
        raw_name,
    )


def _parse_odds(
    sport_code: str, event_id: str, odds_raw: Any
) -> GoalserveOdds:
    """把 inplay feed 的 ``odds`` dict 解析成 GoalserveOdds。

    feed 的 odds 形态：``{market_id: {id, name, suspend, participants:
    {pid: {name, value_eu, handicap, suspend}}}}``。市场带 name，无需推断。

    隐含概率 = 1 / value_eu，量化到 4 位小数。value_eu <= 0（盘口关闭占位）
    的 outcome 跳过——0 赔率无定价意义。
    """
    if not isinstance(odds_raw, dict):
        return GoalserveOdds(event_id=event_id, markets=())

    markets: list[GoalserveMarket] = []
    for market in odds_raw.values():
        if not isinstance(market, dict):
            continue
        market_id = _int_val(market.get("id")) or 0
        raw_name = _str_val(market.get("name"))
        name = _normalize_market_name(sport_code, raw_name)
        _warn_if_unrecognized_market(sport_code, raw_name, name)
        suspended = _flag(market.get("suspend"))

        participants = market.get("participants")
        if not isinstance(participants, dict):
            participants = {}
        outcomes: list[GoalserveOutcome] = []
        for part in participants.values():
            if not isinstance(part, dict):
                continue
            eu = _dec_val(part.get("value_eu"))
            if eu is None or eu <= 0:
                continue
            implied = (Decimal("1") / eu).quantize(Decimal("0.0001"))
            raw_oname = _str_val(part.get("name"))
            oname = _OUTCOME_NAME_NORMALIZE.get(raw_oname.lower(), raw_oname)
            outcomes.append(
                GoalserveOutcome(
                    name=oname,
                    value_eu=eu,
                    implied_prob=implied,
                    handicap=_str_val(part.get("handicap")),
                    suspended=_flag(part.get("suspend")),
                )
            )
        markets.append(
            GoalserveMarket(
                market_id=market_id,
                name=name,
                suspended=suspended,
                outcomes=tuple(outcomes),
            )
        )
    return GoalserveOdds(event_id=event_id, markets=tuple(markets))


# ---------------------------------------------------------------------------
# 共用 LiveEvent 组装
# ---------------------------------------------------------------------------


def _tennis_surname(name: str) -> str | None:
    """提取网球选手姓氏供 Participant.short_name 用于市场文本匹配。

    Goalserve 网球选手名形态多样："Victoria Mboko" / "Mboko V." / "V. Mboko"
    → 均取去掉首字母缩写后的最后一个有效词。
    """
    parts = [p.rstrip(".") for p in name.strip().split()]
    meaningful = [p for p in parts if len(p) > 1]
    if meaningful:
        return meaningful[-1]
    return parts[-1] if parts else None


def _base_event(
    *,
    sport_code: str,
    event_id: str,
    info: dict[str, Any],
    team_info: dict[str, Any],
    status: SportsLiveGameStatus,
    home_score: int | None,
    away_score: int | None,
    odds: GoalserveOdds,
    observed_at: datetime,
    is_tennis: bool = False,
    **state_kwargs: Any,
) -> LiveEvent:
    """组装所有运动共用的 LiveEvent 骨架。"""
    home_name, away_name = _team_names(info, team_info)
    league = _str_val(info.get("league"))
    period = _str_val(info.get("period"))
    ext = {"goalserve": event_id}
    home_short = _tennis_surname(home_name) if is_tennis else None
    away_short = _tennis_surname(away_name) if is_tennis else None
    return LiveEvent(
        source=SOURCE,
        source_event_id=event_id,
        kind=LiveEventKind.TEAM_MATCH,
        league=league,
        sport=sport_code,
        participants=(
            Participant(
                role="home",
                name=home_name,
                score=home_score,
                short_name=home_short,
                external_ids=dict(ext),
            ),
            Participant(
                role="away",
                name=away_name,
                score=away_score,
                short_name=away_short,
                external_ids=dict(ext),
            ),
        ),
        status=status,
        period=period,
        raw_status=period or None,
        observed_at=observed_at,
        event_start_time=_parse_start_time(info),
        event_name=f"{home_name} vs {away_name}",
        external_ids=dict(ext),
        source_payload={"goalserve_odds": odds.as_dict()},
        **state_kwargs,
    )


# ---------------------------------------------------------------------------
# Soccer
# ---------------------------------------------------------------------------

# info.period 文本 → SoccerGameState.period 规范值。
_SOCCER_PERIOD_MAP: dict[str, str] = {
    "1st half": "first_half",
    "first half": "first_half",
    "half time": "half_time",
    "halftime": "half_time",
    "2nd half": "second_half",
    "second half": "second_half",
    "extra time": "extra_time",
    "1st extra": "extra_time",
    "2nd extra": "extra_time",
    "penalties": "penalties",
    "penalty shootout": "penalties",
}


def _parse_soccer_event(
    event_id: str, event: dict[str, Any], observed_at: datetime
) -> LiveEvent:
    core = event.get("core") or {}
    info = event.get("info") or {}
    team_info = event.get("team_info") or {}
    status = _map_status(core, info)
    # team_info.score 是字符串比分；缺失时回退 info.score "H:A"。
    home_score = _team_score(team_info, "home")
    away_score = _team_score(team_info, "away")
    if home_score is None or away_score is None:
        info_h, info_a = _score_from_info(info)
        home_score = home_score if home_score is not None else info_h
        away_score = away_score if away_score is not None else info_a

    period_raw = _str_val(info.get("period")).lower()
    soccer_period = _SOCCER_PERIOD_MAP.get(period_raw)
    minute = _int_val(info.get("minute"))
    # 半场比分：stats row name "IFirstHalfScore" 给出（实测软件足球行用 I 前缀）。
    ht_row = _stat_row_by_name(event, "IFirstHalfScore")
    ht_home = _int_val(ht_row.get("home")) if ht_row else None
    ht_away = _int_val(ht_row.get("away")) if ht_row else None
    # 红/黄牌 stats row。
    red_row = _stat_row_by_name(event, "IRedCard")
    yellow_row = _stat_row_by_name(event, "IYellowCard")
    soccer_state = SoccerGameState(
        period=soccer_period,
        clock_minutes=minute,
        home_red_cards=_int_val((red_row or {}).get("home")) or 0,
        away_red_cards=_int_val((red_row or {}).get("away")) or 0,
        home_yellow_cards=_int_val((yellow_row or {}).get("home")) or 0,
        away_yellow_cards=_int_val((yellow_row or {}).get("away")) or 0,
        home_halftime_score=ht_home,
        away_halftime_score=ht_away,
    )
    odds = _parse_odds("soccer", event_id, event.get("odds"))
    return _base_event(
        sport_code="soccer",
        event_id=event_id,
        info=info,
        team_info=team_info,
        status=status,
        home_score=home_score,
        away_score=away_score,
        odds=odds,
        observed_at=observed_at,
        soccer_state=soccer_state,
    )


# ---------------------------------------------------------------------------
# Basketball
# ---------------------------------------------------------------------------


def _basketball_current_period(period_raw: str) -> int | None:
    """从 info.period 文本判定当前节。"Halftime" → 3（上半场已结束）。"""
    s = period_raw.lower()
    if "halftime" in s or "half time" in s:
        return 3
    if "overtime" in s or " ot" in s or s == "ot":
        return 5
    for period, token in ((1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th")):
        if token in s:
            return period
    return None


def _basketball_quarter_scores(event: dict[str, Any]) -> tuple[tuple[int | None, ...], tuple[int | None, ...]]:
    """从 stats 的分节行（name "1".."4"）提取各节得分。

    实测 stats row：name="1".."4" 是各节得分，"OT" 加时，"Half" 上半场，"T" 总分。
    """
    home: list[int | None] = []
    away: list[int | None] = []
    for q in ("1", "2", "3", "4"):
        row = _stat_row_by_name(event, q)
        home.append(_int_val((row or {}).get("home")))
        away.append(_int_val((row or {}).get("away")))
    return tuple(home), tuple(away)


def _parse_basketball_event(
    event_id: str, event: dict[str, Any], observed_at: datetime
) -> LiveEvent:
    core = event.get("core") or {}
    info = event.get("info") or {}
    team_info = event.get("team_info") or {}
    status = _map_status(core, info)
    home_score = _team_score(team_info, "home")
    away_score = _team_score(team_info, "away")
    if home_score is None or away_score is None:
        # team_info.score 缺失时回退 stats "T" 行（总分）再回退 info.score。
        t_row = _stat_row_by_name(event, "T")
        if t_row is not None:
            home_score = home_score if home_score is not None else _int_val(t_row.get("home"))
            away_score = away_score if away_score is not None else _int_val(t_row.get("away"))
        info_h, info_a = _score_from_info(info)
        home_score = home_score if home_score is not None else info_h
        away_score = away_score if away_score is not None else info_a

    home_q, away_q = _basketball_quarter_scores(event)
    # 只要底层 stats 行存在就构造 GameState——暂停（stopped）/未结束赛事同样
    # 携带分节比分，不因状态非 LIVE 而丢弃可用数据。仅在 stats 完全缺失时为 None。
    has_stats = any(q is not None for q in (*home_q, *away_q))
    basketball_state = (
        BasketballGameState(
            current_period=_basketball_current_period(_str_val(info.get("period"))),
            home_quarter_scores=home_q,
            away_quarter_scores=away_q,
        )
        if has_stats
        else None
    )
    odds = _parse_odds("basketball", event_id, event.get("odds"))
    return _base_event(
        sport_code="basketball",
        event_id=event_id,
        info=info,
        team_info=team_info,
        status=status,
        home_score=home_score,
        away_score=away_score,
        odds=odds,
        observed_at=observed_at,
        basketball_state=basketball_state,
    )


# ---------------------------------------------------------------------------
# Tennis
# ---------------------------------------------------------------------------


def _tennis_state(event: dict[str, Any], info: dict[str, Any]) -> TennisGameState:
    """从 stats 行构造 TennisGameState。

    实测 stats row：name="T" 已赢盘数 [home,away]，"S1".."S5" 各盘局分，
    "POINTS" 当前局分。info.period "Set 3" 给当前盘号。
    """
    t_row = _stat_row_by_name(event, "T")
    home_sets = _int_val((t_row or {}).get("home")) or 0
    away_sets = _int_val((t_row or {}).get("away")) or 0

    set_scores: list[tuple[int, int]] = []
    for i in range(1, 6):
        row = _stat_row_by_name(event, f"S{i}")
        if row is None:
            break
        hs = _int_val(row.get("home"))
        as_ = _int_val(row.get("away"))
        if hs is None and as_ is None:
            break
        set_scores.append((hs or 0, as_ or 0))

    period_raw = _str_val(info.get("period"))
    m = re.search(r"set\s*(\d+)", period_raw.lower())
    current_set = int(m.group(1)) if m else (len(set_scores) or 1)
    home_cur: int | None = None
    away_cur: int | None = None
    if 1 <= current_set <= len(set_scores):
        home_cur, away_cur = set_scores[current_set - 1]

    points_row = _stat_row_by_name(event, "POINTS")
    home_point = _str_val((points_row or {}).get("home")) or None
    away_point = _str_val((points_row or {}).get("away")) or None

    return TennisGameState(
        home_sets_won=home_sets,
        away_sets_won=away_sets,
        current_set=current_set,
        home_current_set_games=home_cur,
        away_current_set_games=away_cur,
        set_scores=tuple(set_scores),
        home_point=home_point,
        away_point=away_point,
    )


def _parse_tennis_event(
    event_id: str, event: dict[str, Any], observed_at: datetime
) -> LiveEvent:
    core = event.get("core") or {}
    info = event.get("info") or {}
    team_info = event.get("team_info") or {}
    status = _map_status(core, info)
    tennis_state = _tennis_state(event, info)
    # 网球 LiveEvent.score 用已赢盘数（与 livescore/WS parser 一致）。
    odds = _parse_odds("tennis", event_id, event.get("odds"))
    return _base_event(
        sport_code="tennis",
        event_id=event_id,
        info=info,
        team_info=team_info,
        status=status,
        home_score=tennis_state.home_sets_won,
        away_score=tennis_state.away_sets_won,
        odds=odds,
        observed_at=observed_at,
        is_tennis=True,
        tennis_state=tennis_state,
    )


# ---------------------------------------------------------------------------
# Volleyball
# ---------------------------------------------------------------------------


def _volleyball_state(event: dict[str, Any], info: dict[str, Any]) -> VolleyballGameState:
    """从 stats 行构造 VolleyballGameState（与网球同构：T=盘数，S1..S5=各盘分）。"""
    t_row = _stat_row_by_name(event, "T")
    home_sets = _int_val((t_row or {}).get("home")) or 0
    away_sets = _int_val((t_row or {}).get("away")) or 0

    set_scores: list[tuple[int, int]] = []
    for i in range(1, 6):
        row = _stat_row_by_name(event, f"S{i}")
        if row is None:
            break
        hs = _int_val(row.get("home"))
        as_ = _int_val(row.get("away"))
        if hs is None and as_ is None:
            break
        set_scores.append((hs or 0, as_ or 0))

    period_raw = _str_val(info.get("period"))
    m = re.search(r"set\s*(\d+)", period_raw.lower())
    current_set = int(m.group(1)) if m else (len(set_scores) or 1)
    home_cur: int | None = None
    away_cur: int | None = None
    if 1 <= current_set <= len(set_scores):
        home_cur, away_cur = set_scores[current_set - 1]

    return VolleyballGameState(
        home_sets_won=home_sets,
        away_sets_won=away_sets,
        current_set=current_set,
        home_current_set_points=home_cur,
        away_current_set_points=away_cur,
        set_scores=tuple(set_scores),
    )


def _parse_volleyball_event(
    event_id: str, event: dict[str, Any], observed_at: datetime
) -> LiveEvent:
    core = event.get("core") or {}
    info = event.get("info") or {}
    team_info = event.get("team_info") or {}
    status = _map_status(core, info)
    vb_state = _volleyball_state(event, info)
    odds = _parse_odds("volleyball", event_id, event.get("odds"))
    return _base_event(
        sport_code="volleyball",
        event_id=event_id,
        info=info,
        team_info=team_info,
        status=status,
        home_score=vb_state.home_sets_won,
        away_score=vb_state.away_sets_won,
        odds=odds,
        observed_at=observed_at,
        volleyball_state=vb_state,
    )


# ---------------------------------------------------------------------------
# Esports
# ---------------------------------------------------------------------------


def _parse_best_of(period_raw: str, odds: GoalserveOdds) -> int | None:
    """推断 best-of 局数。

    inplay feed 的 info.period 实测是 "Started"（无 BOn）——没有显式 best-of 字段。
    回退：从 odds 市场名里的 "(map N)" 取最大 N 推断系列赛长度（map 2 存在 → 至少 BO3）。
    无法确定时返回 None，下游据此给可审计拒绝原因。
    """
    m = re.search(r"bo\s*(\d+)", period_raw.lower())
    if m is not None:
        value = int(m.group(1))
        return value if value > 0 else None
    max_map = 0
    for market in odds.markets:
        mm = re.search(r"map\s*(\d+)", market.name.lower())
        if mm is not None:
            max_map = max(max_map, int(mm.group(1)))
    if max_map >= 2:
        # 出现 map N 盘口 → 系列赛至少能打到 N 局，BO 至少 2N-1。
        return 2 * max_map - 1
    return None


def _parse_esports_event(
    event_id: str, event: dict[str, Any], observed_at: datetime
) -> LiveEvent:
    core = event.get("core") or {}
    info = event.get("info") or {}
    team_info = event.get("team_info") or {}
    status = _map_status(core, info)
    # 电竞已赢局数（maps won）来自 stats row name="Res"——team_info.score 实测为 null。
    res_row = _stat_row_by_name(event, "Res")
    home_maps = _int_val((res_row or {}).get("home")) or 0
    away_maps = _int_val((res_row or {}).get("away")) or 0
    odds = _parse_odds("esports", event_id, event.get("odds"))
    best_of = _parse_best_of(_str_val(info.get("period")), odds)
    esports_state = EsportsGameState(
        best_of=best_of,
        home_maps_won=home_maps,
        away_maps_won=away_maps,
    )
    return _base_event(
        sport_code="esports",
        event_id=event_id,
        info=info,
        team_info=team_info,
        status=status,
        home_score=home_maps,
        away_score=away_maps,
        odds=odds,
        observed_at=observed_at,
        esports_state=esports_state,
    )


# ---------------------------------------------------------------------------
# Baseball
# ---------------------------------------------------------------------------


def _baseball_inning_runs(event: dict[str, Any]) -> tuple[tuple[int | None, ...], tuple[int | None, ...]]:
    """从 stats 逐局行（name "1".."15"）提取各局得分。

    下标 0 = 第 1 局；该局未开始时 home/away 为 null → None。固定取 1..9 局
    （加时局极少且分局盘口只覆盖前 9 局）。
    """
    home: list[int | None] = []
    away: list[int | None] = []
    for i in range(1, 10):
        row = _stat_row_by_name(event, str(i))
        home.append(_int_val((row or {}).get("home")))
        away.append(_int_val((row or {}).get("away")))
    return tuple(home), tuple(away)


def _baseball_inning_half(event: dict[str, Any], info: dict[str, Any]) -> tuple[int | None, str | None]:
    """解析当前局数与上/下半局。

    info.period "Inning 5" 给局数；sts 字段 "1|Top of 5th|..." 第 2 段给上下半局。
    """
    inning: int | None = None
    period = _str_val(info.get("period")).lower()
    m = re.search(r"inning\s*(\d+)", period)
    if m is not None:
        inning = int(m.group(1))

    half: str | None = None
    sts = _str_val(event.get("sts"))
    if sts:
        # sts 形如 "1|Top of 5th|0|0|..."——第 2 段含 "Top"/"Bottom"。
        segs = sts.split("|")
        if len(segs) >= 2:
            half_seg = segs[1].lower()
            if "top" in half_seg:
                half = "top"
            elif "bot" in half_seg:
                half = "bottom"
            if inning is None:
                im = re.search(r"(\d+)", half_seg)
                if im is not None:
                    inning = int(im.group(1))
    return inning, half


def _parse_baseball_event(
    event_id: str, event: dict[str, Any], observed_at: datetime
) -> LiveEvent:
    core = event.get("core") or {}
    info = event.get("info") or {}
    team_info = event.get("team_info") or {}
    status = _map_status(core, info)
    home_score = _team_score(team_info, "home")
    away_score = _team_score(team_info, "away")
    if home_score is None or away_score is None:
        # 回退 stats "R"（runs 总分）再回退 info.score。
        r_row = _stat_row_by_name(event, "R")
        if r_row is not None:
            home_score = home_score if home_score is not None else _int_val(r_row.get("home"))
            away_score = away_score if away_score is not None else _int_val(r_row.get("away"))
        info_h, info_a = _score_from_info(info)
        home_score = home_score if home_score is not None else info_h
        away_score = away_score if away_score is not None else info_a

    inning, half = _baseball_inning_half(event, info)
    home_runs, away_runs = _baseball_inning_runs(event)
    baseball_state = BaseballGameState(
        current_inning=inning,
        inning_half=half,
        home_inning_runs=home_runs,
        away_inning_runs=away_runs,
    )
    odds = _parse_odds("baseball", event_id, event.get("odds"))
    return _base_event(
        sport_code="baseball",
        event_id=event_id,
        info=info,
        team_info=team_info,
        status=status,
        home_score=home_score,
        away_score=away_score,
        odds=odds,
        observed_at=observed_at,
        baseball_state=baseball_state,
    )


# ---------------------------------------------------------------------------
# Hockey / American Football
# ---------------------------------------------------------------------------


def _parse_hockey_event(
    event_id: str, event: dict[str, Any], observed_at: datetime
) -> LiveEvent:
    """冰球：无专用 GameState，比分取 team_info / info.score。

    注：抓取窗口内 inplay-hockey 暂无 live 赛事，格式按通用 EVENT 结构对齐
    （core/info/team_info 同构于篮球/棒球），首次有真实数据后再校准 stats 行。
    """
    core = event.get("core") or {}
    info = event.get("info") or {}
    team_info = event.get("team_info") or {}
    status = _map_status(core, info)
    home_score = _team_score(team_info, "home")
    away_score = _team_score(team_info, "away")
    if home_score is None or away_score is None:
        info_h, info_a = _score_from_info(info)
        home_score = home_score if home_score is not None else info_h
        away_score = away_score if away_score is not None else info_a
    odds = _parse_odds("ice-hockey", event_id, event.get("odds"))
    return _base_event(
        sport_code="ice-hockey",
        event_id=event_id,
        info=info,
        team_info=team_info,
        status=status,
        home_score=home_score,
        away_score=away_score,
        odds=odds,
        observed_at=observed_at,
    )


def _parse_amfootball_event(
    event_id: str, event: dict[str, Any], observed_at: datetime
) -> LiveEvent:
    """美式橄榄球：无专用 GameState，比分取 team_info / info.score。

    注：抓取窗口内 inplay-amfootball 暂无 live 赛事，格式按通用 EVENT 结构对齐，
    首次有真实数据后再校准 stats 行。
    """
    core = event.get("core") or {}
    info = event.get("info") or {}
    team_info = event.get("team_info") or {}
    status = _map_status(core, info)
    home_score = _team_score(team_info, "home")
    away_score = _team_score(team_info, "away")
    if home_score is None or away_score is None:
        info_h, info_a = _score_from_info(info)
        home_score = home_score if home_score is not None else info_h
        away_score = away_score if away_score is not None else info_a
    odds = _parse_odds("american-football", event_id, event.get("odds"))
    return _base_event(
        sport_code="american-football",
        event_id=event_id,
        info=info,
        team_info=team_info,
        status=status,
        home_score=home_score,
        away_score=away_score,
        odds=odds,
        observed_at=observed_at,
    )


# ---------------------------------------------------------------------------
# 顶层分派
# ---------------------------------------------------------------------------

# inplay feed sport 路径 token → 单事件 parser。
_EVENT_PARSERS = {
    "soccer": _parse_soccer_event,
    "basket": _parse_basketball_event,
    "tennis": _parse_tennis_event,
    "volleyball": _parse_volleyball_event,
    "esports": _parse_esports_event,
    "baseball": _parse_baseball_event,
    "hockey": _parse_hockey_event,
    "amfootball": _parse_amfootball_event,
}


def parse_goalserve_inplay(
    sport: str,
    feed_dict: dict[str, Any],
    observed_at: datetime | None = None,
) -> list[LiveEvent]:
    """把一个 inplay GZIP feed dict 解析成 LiveEvent 列表。

    sport 为 feed 路径 token（soccer / basket / tennis / volleyball / amfootball /
    esports / hockey / baseball）。feed_dict 为 gunzip + json.loads 后的根节点。
    ``events`` 为空 dict（当前无 live 赛事）时返回空列表——这是正常态，不是错误。
    observed_at 为 None 时取当前 UTC 时间。

    停止/已结束的赛事不在此过滤——状态由 _map_status 标记，由上层决定如何处理；
    parser 保持纯函数，不做"该不该交易"的判断。
    """
    ts = observed_at or utc_now()
    if not isinstance(feed_dict, dict):
        return []
    events = feed_dict.get("events")
    if not isinstance(events, dict) or not events:
        return []
    parser = _EVENT_PARSERS.get(sport.strip().lower())
    if parser is None:
        return []

    results: list[LiveEvent] = []
    for match_id, event in events.items():
        if not isinstance(event, dict):
            continue
        event_id = str(match_id)
        try:
            results.append(parser(event_id, event, ts))
        except Exception:
            # 单场解析失败不阻断其他赛事——inplay feed 字段偶有缺失，
            # 跳过坏数据而非整批丢弃。
            continue
    return results
