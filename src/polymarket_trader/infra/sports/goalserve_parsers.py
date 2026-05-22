"""Goalserve inplay WebSocket per-sport parsers。

把 GoalserveClient._state 快照（{event_id: ws_message_dict}）转换成 LiveEvent 列表。

WS 消息关键字段：
  t1.n / t2.n        主客队名称
  stp                time_status 整数：0=未开始, 1=进行中, 3=已结束,
                     4=延期, 5=取消, 7=暂停/中断, 99=已移除（客户端层已过滤）
  et                 已进行秒数（elapsed seconds）
  stats.g            [home_score, away_score]（进球/得分/运动类型相关）
  stats.y/r/c        黄牌/红牌/角球 [home, away]（足球）
  ctry_name          联赛/赛事名称
  st                 开赛 epoch 秒（整数，非毫秒）
  odds               盘口列表（list）
  sc                 state code（透传为 raw_status）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

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

# stp 整数 → SportsLiveGameStatus 映射。
# 注意：inplay WS 是"进行中赛事流"——能进到这个 feed 的赛事就是在打。实测带
# 比分/赔率的 avl/updt 消息 stp=0，因此 stp=0 表示 LIVE（不是 scheduled）；
# stp 终态 3/4/5（ended/postponed/cancelled）、99（removed，已在 client 过滤）。
# 历史上 0→SCHEDULED 是错的，会把所有 inplay 直播赛事误判为未开赛。
_STP_STATUS: dict[int, SportsLiveGameStatus] = {
    0: SportsLiveGameStatus.LIVE,
    1: SportsLiveGameStatus.LIVE,
    3: SportsLiveGameStatus.ENDED,
    4: SportsLiveGameStatus.POSTPONED,
    5: SportsLiveGameStatus.CANCELLED,
    7: SportsLiveGameStatus.PAUSED,
}


def _stp_to_status(stp: Any) -> SportsLiveGameStatus:
    try:
        return _STP_STATUS.get(int(stp), SportsLiveGameStatus.UNKNOWN)
    except (TypeError, ValueError):
        return SportsLiveGameStatus.UNKNOWN


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


def _parse_start_time(ts: Any) -> datetime | None:
    """WS 给出 epoch 秒（整数），老 HTTP feed 给的是毫秒——这里只处理秒。"""
    if not ts:
        return None
    try:
        s = int(str(ts).strip())
        return datetime.fromtimestamp(s, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _score_pair(stats: dict[str, Any], key: str) -> tuple[int | None, int | None]:
    """从 stats 取 [home, away] 对并返回 int 元组。"""
    pair = stats.get(key)
    if not isinstance(pair, (list, tuple)) or len(pair) < 2:
        return None, None
    return _int_val(pair[0]), _int_val(pair[1])


def _set_based_state(
    stats: dict[str, Any], pc: Any
) -> tuple[int, int, tuple[tuple[int, int], ...], int | None, int | None, int | None]:
    """从 inplay WS 的盘制 stats（tennis / volleyball）提取盘分。

    实测格式（已对真实 WS 消息校准）：
    - ``stats.T``  = [已赢盘数_主, 已赢盘数_客]
    - ``stats.S1``..``S5`` = 各盘局分/分数 [主, 客]
    - ``pc`` = 当前进行的盘号

    返回 (home_sets, away_sets, set_scores, current_set, home_cur, away_cur)。
    """
    home_sets, away_sets = _score_pair(stats, "T")
    set_scores: list[tuple[int, int]] = []
    for i in range(1, 6):
        h, a = _score_pair(stats, f"S{i}")
        if h is None and a is None:
            break
        set_scores.append((h or 0, a or 0))
    current_set = _int_val(pc)
    home_cur: int | None = None
    away_cur: int | None = None
    if current_set is not None and 1 <= current_set <= len(set_scores):
        home_cur, away_cur = set_scores[current_set - 1]
    return (home_sets or 0, away_sets or 0, tuple(set_scores), current_set, home_cur, away_cur)


def _infer_ws_market_name(outcome_names: list[str], handicap: str) -> str:
    """WS inplay 赔率市场只有数字 id、没有名字——按结果集与盘口线推断类型名，

    使下游 ``_extract_goalserve_*`` 能按名称识别（moneyline/totals/spread）。
    """
    names = {n.strip().lower() for n in outcome_names if n}
    if not names:
        return ""
    has_handicap = bool(handicap) and handicap.strip() not in ("", "0", "0.0", "-0", "-0.0")
    if names <= {"over", "under"}:
        return "over/under"
    if names <= {"1", "2", "home", "away"}:
        return "handicap" if has_handicap else "money line"
    if names <= {"yes", "no"}:
        return "yes/no"
    return ""


def _parse_odds_ws(odds_raw: Any, event_id: str) -> GoalserveOdds:
    """解析 WS odds 列表 → GoalserveOdds。

    WS odds 实测格式：市场 ``{id, ha(盘口线), o(outcomes)}``——市场无名字；
    outcome ``{n(name), v(decimal odds), lv, b}``。盘口线 ``ha`` 在市场级。
    市场名从结果集推断（见 _infer_ws_market_name）。兼容长键以防服务端变更。
    """
    if not isinstance(odds_raw, list):
        return GoalserveOdds(event_id=event_id, markets=())

    markets: list[GoalserveMarket] = []
    for item in odds_raw:
        if not isinstance(item, dict):
            continue
        mkt_id = _int_val(item.get("id")) or 0
        mkt_susp = bool(_int_val(item.get("sp") or item.get("suspend", 0)))
        # 盘口线在市场级 ha；兼容旧 handicap 键。
        ha_raw = item.get("ha")
        handicap = str(ha_raw if ha_raw is not None else (item.get("handicap") or "") or "")

        outcomes: list[GoalserveOutcome] = []
        outcome_names: list[str] = []
        for od in item.get("o", []):
            if not isinstance(od, dict):
                continue
            eu = _dec_val(od.get("v") if od.get("v") is not None else od.get("value_eu"))
            if eu is None or eu <= 0:
                continue
            implied = (Decimal("1") / eu).quantize(Decimal("0.0001"))
            # 结果名实测键为 n；盘口线取结果级 hc，缺失回退市场级 ha。
            oname = str(od.get("n") or od.get("nm") or od.get("name", ""))
            outcome_names.append(oname)
            outcomes.append(
                GoalserveOutcome(
                    name=oname,
                    value_eu=eu,
                    implied_prob=implied,
                    handicap=str(od.get("hc") or od.get("handicap") or handicap or ""),
                    suspended=bool(_int_val(od.get("sp") or od.get("suspend", 0))),
                )
            )
        # WS 市场无名字 → 从结果集推断；服务端若日后给 nm/name 则优先用。
        name = str(item.get("nm") or item.get("name") or "") or _infer_ws_market_name(
            outcome_names, handicap
        )
        markets.append(
            GoalserveMarket(
                market_id=mkt_id,
                name=name,
                suspended=mkt_susp,
                outcomes=tuple(outcomes),
            )
        )
    return GoalserveOdds(event_id=event_id, markets=tuple(markets))


# Goalserve soccer inplay WS 的 pc（period code）实测值：
#   1 = 上半场, 2 = 中场休息, 3 = 下半场, 4/5 = 加时上/下半场。
# 比 et 阈值更可靠（Esoccer 压缩赛制下 et 秒数被加速，阈值会误判赛段）。
_SOCCER_PC_PERIOD: dict[int, str] = {
    1: "first_half",
    2: "half_time",
    3: "second_half",
    4: "extra_time",
    5: "extra_time",
}


def _pc_to_soccer_period(pc: Any) -> str | None:
    """把 inplay WS 的 period code（pc）映射到足球 period 字符串。"""
    code = _int_val(pc)
    if code is None:
        return None
    return _SOCCER_PC_PERIOD.get(code)


def _et_to_soccer_period(et: int | None) -> str | None:
    """从 elapsed seconds 推断足球 period（仅作 pc 缺失时的回退）。"""
    if et is None:
        return None
    if et < 2700:
        return "first_half"
    if et < 5400:
        return "second_half"
    return "extra_time"


# Goalserve soccer inplay WS 的 cms（commentary）事件类型码：
#   mt="255" = 进球（含点球进球）。ti="1"=主队, ti="2"=客队。
# 实测 stats.g 恒为 [0,0]（无效）；stat 字符串也不含进球 token。
# 因此 WS 足球比分唯一可靠来源是 cms 里的 mt=255 计数（已对 livescore 校准）。
_SOCCER_GOAL_EVENT_TYPE = "255"


def _soccer_score_from_cms(cms: Any) -> tuple[int, int]:
    """从 inplay WS 的 cms 进球事件统计足球比分。

    实测 ``stats.g`` 恒 [0,0]、``stat`` 字符串无进球字段——唯一可靠来源是
    cms 里 ``mt="255"`` 的进球事件，``ti`` 标识进球方（"1"主/"2"客）。
    与 livescore getfeed 同场比分交叉校准一致。
    """
    home = away = 0
    if not isinstance(cms, list):
        return 0, 0
    for item in cms:
        if not isinstance(item, dict):
            continue
        if str(item.get("mt")) != _SOCCER_GOAL_EVENT_TYPE:
            continue
        ti = str(item.get("ti", ""))
        if ti == "1":
            home += 1
        elif ti == "2":
            away += 1
    return home, away


# 各运动比赛时钟上限（秒）——用于从 et（已打比赛时钟秒数）推算剩余时间。
# 注意：此处只返回纯比赛时钟剩余秒数，不加停伤停/补时缓冲。
# 结算等待缓冲已由调用方 _estimated_settlement_hold_minutes 的 buffer 参数处理，
# 避免双重叠加导致 entry 门禁（seconds_remaining <= 阈值）判断过于保守。
_SOCCER_REGULATION_SECONDS = 5400       # 90 分钟
_SOCCER_EXTRA_TIME_SECONDS = 1800       # 加时赛 30 分钟（上下半场各 15 分钟）
_BASKETBALL_REGULATION_SECONDS = 2880   # NBA 48 分钟；欧洲联赛 40 分钟取最大值
_BASKETBALL_OVERTIME_BUFFER_SECONDS = 300  # 加时赛 5 分钟
_HOCKEY_REGULATION_SECONDS = 3600       # NHL/冰球 60 分钟
_AMFOOTBALL_REGULATION_SECONDS = 3600   # NFL/CFL/大学橄榄球 60 分钟比赛时钟


def _soccer_seconds_remaining(et: int | None, status: SportsLiveGameStatus) -> int | None:
    """从已进行比赛时钟秒数估算足球剩余秒数（纯比赛时钟，不含补时缓冲）。

    et 是比赛时钟已进行秒数，不是挂钟时间。
    - 规则时间内：剩余 = 5400 - et（entry 门禁校验和持仓时间估算分别用此值）
    - 加时赛中（et >= 5400）：剩余 = 加时赛总时间 - 已超出规则时间
    """
    if et is None or status != SportsLiveGameStatus.LIVE:
        return None
    if et < _SOCCER_REGULATION_SECONDS:
        return max(0, _SOCCER_REGULATION_SECONDS - et)
    # 加时赛阶段
    extra_elapsed = et - _SOCCER_REGULATION_SECONDS
    remaining = _SOCCER_EXTRA_TIME_SECONDS - extra_elapsed
    return max(0, remaining)


def _basketball_seconds_remaining(et: int | None, status: SportsLiveGameStatus) -> int | None:
    """从已进行比赛时钟秒数估算篮球剩余秒数（纯比赛时钟）。

    et 是比赛时钟已进行秒数（NBA 比赛共 2880 秒，欧洲联赛 2400 秒）。
    超出规则时间则说明进入加时赛，保守给 5 分钟。
    """
    if et is None or status != SportsLiveGameStatus.LIVE:
        return None
    if et < _BASKETBALL_REGULATION_SECONDS:
        return max(0, _BASKETBALL_REGULATION_SECONDS - et)
    return _BASKETBALL_OVERTIME_BUFFER_SECONDS


def _hockey_seconds_remaining(et: int | None, status: SportsLiveGameStatus) -> int | None:
    """从已进行比赛时钟秒数估算冰球剩余秒数（纯比赛时钟）。"""
    if et is None or status != SportsLiveGameStatus.LIVE:
        return None
    if et < _HOCKEY_REGULATION_SECONDS:
        return max(0, _HOCKEY_REGULATION_SECONDS - et)
    return 300  # 加时赛：NHL 5 分钟 OT


def _amfootball_seconds_remaining(et: int | None, status: SportsLiveGameStatus) -> int | None:
    """从已进行比赛时钟秒数估算橄榄球剩余秒数（纯比赛时钟）。

    注意：橄榄球终盘 2 分钟时钟暂停频繁，实际挂钟时间可能远超时钟剩余时间，
    settlement buffer 由调用方处理。
    """
    if et is None or status != SportsLiveGameStatus.LIVE:
        return None
    if et < _AMFOOTBALL_REGULATION_SECONDS:
        return max(0, _AMFOOTBALL_REGULATION_SECONDS - et)
    return 300  # 加时赛 OT 5 分钟


# ---------------------------------------------------------------------------
# Tennis player name helpers
# ---------------------------------------------------------------------------

def _tennis_surname(name: str) -> str | None:
    """提取网球选手姓氏（去除首字母缩写词后的最后一词）。

    Goalserve WS 格式多样："N. Djokovic" / "Djokovic N." / "Novak Djokovic"
    → 均返回 "Djokovic"，供 Participant.short_name 用于市场文本匹配。
    """
    parts = [p.rstrip(".") for p in name.strip().split()]
    meaningful = [p for p in parts if len(p) > 1]
    return meaningful[-1] if meaningful else (parts[-1] if parts else None)


# ---------------------------------------------------------------------------
# Basketball
# ---------------------------------------------------------------------------


def _parse_basketball(state_dict: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in state_dict.items():
        stp = ev.get("stp", 0)
        status = _stp_to_status(stp)
        stats = ev.get("stats", {})
        home_score, away_score = _score_pair(stats, "g")
        et = _int_val(ev.get("et"))
        odds = _parse_odds_ws(ev.get("odds", []), event_id)
        home_name = ev.get("t1", {}).get("n", "")
        away_name = ev.get("t2", {}).get("n", "")
        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=ev.get("ctry_name", ""),
                sport="basketball",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=str(ev.get("sc", "")),
                seconds_remaining=_basketball_seconds_remaining(et, status),
                event_name=f"{home_name} vs {away_name}",
                event_start_time=_parse_start_time(ev.get("st")),
                external_ids={"goalserve": event_id},
                raw_status=str(stp),
                observed_at=observed_at,
                source_payload={"goalserve_odds": odds.as_dict()},
            )
        )
    return results


# ---------------------------------------------------------------------------
# Soccer
# ---------------------------------------------------------------------------


def _parse_soccer(state_dict: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in state_dict.items():
        stp = ev.get("stp", 0)
        status = _stp_to_status(stp)
        stats = ev.get("stats", {})
        # 进球比分来自 cms（mt=255）——stats.g 实测恒 [0,0] 无效。
        home_score, away_score = _soccer_score_from_cms(ev.get("cms"))
        # 黄/红牌 stats.y/stats.r 实测有效（与 stat 字符串 YELLOW_CARD/RED_CARD 一致）。
        home_yellow, away_yellow = _score_pair(stats, "y")
        home_red, away_red = _score_pair(stats, "r")
        et = _int_val(ev.get("et"))
        period = _pc_to_soccer_period(ev.get("pc")) or _et_to_soccer_period(et)
        soccer_state = SoccerGameState(
            period=period,
            clock_minutes=et // 60 if et is not None else None,
            home_red_cards=home_red or 0,
            away_red_cards=away_red or 0,
            home_yellow_cards=home_yellow or 0,
            away_yellow_cards=away_yellow or 0,
        )
        odds = _parse_odds_ws(ev.get("odds", []), event_id)
        home_name = ev.get("t1", {}).get("n", "")
        away_name = ev.get("t2", {}).get("n", "")
        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=ev.get("ctry_name", ""),
                sport="soccer",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=period or "",
                seconds_remaining=_soccer_seconds_remaining(et, status),
                event_name=f"{home_name} vs {away_name}",
                event_start_time=_parse_start_time(ev.get("st")),
                external_ids={"goalserve": event_id},
                raw_status=str(ev.get("sc", stp)),
                observed_at=observed_at,
                soccer_state=soccer_state,
                source_payload={"goalserve_odds": odds.as_dict()},
            )
        )
    return results


# ---------------------------------------------------------------------------
# Hockey
# ---------------------------------------------------------------------------


def _parse_hockey(state_dict: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in state_dict.items():
        stp = ev.get("stp", 0)
        status = _stp_to_status(stp)
        stats = ev.get("stats", {})
        home_score, away_score = _score_pair(stats, "g")
        et = _int_val(ev.get("et"))
        odds = _parse_odds_ws(ev.get("odds", []), event_id)
        home_name = ev.get("t1", {}).get("n", "")
        away_name = ev.get("t2", {}).get("n", "")
        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=ev.get("ctry_name", ""),
                sport="ice-hockey",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=str(ev.get("sc", "")),
                seconds_remaining=_hockey_seconds_remaining(et, status),
                event_name=f"{home_name} vs {away_name}",
                event_start_time=_parse_start_time(ev.get("st")),
                external_ids={"goalserve": event_id},
                raw_status=str(stp),
                observed_at=observed_at,
                source_payload={"goalserve_odds": odds.as_dict()},
            )
        )
    return results


# ---------------------------------------------------------------------------
# Baseball
# ---------------------------------------------------------------------------

def _parse_baseball_state(sc_raw: str, status: SportsLiveGameStatus) -> BaseballGameState:
    """尝试从 sc（state code）字段解析当前局数和上/下半局。

    WS 格式未明文文档化，根据已知惯例尝试常见格式：
    - 纯数字（"7"）→ 第 7 局，上半局未知
    - "TOP7" / "T7" → 第 7 局上半
    - "BOT7" / "B7" / "BTM7" → 第 7 局下半
    若解析失败（unknown / not LIVE），返回 None 字段，保持旧行为。
    """
    if status != SportsLiveGameStatus.LIVE or not sc_raw:
        return BaseballGameState(current_inning=None, inning_half=None)
    sc = sc_raw.strip().upper()
    half: str | None = None
    if sc.startswith(("TOP", "T")) and not sc.startswith("T0"):
        half = "top"
        sc = re.sub(r"^(TOP|T)", "", sc)
    elif sc.startswith(("BOT", "BTM", "B")) and not sc.startswith("B0"):
        half = "bottom"
        sc = re.sub(r"^(BOT|BTM|B)", "", sc)
    try:
        inning = int(sc)
        if 1 <= inning <= 20:  # 合理局数范围
            return BaseballGameState(current_inning=inning, inning_half=half)
    except (ValueError, TypeError):
        pass
    return BaseballGameState(current_inning=None, inning_half=None)


def _parse_baseball(state_dict: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in state_dict.items():
        stp = ev.get("stp", 0)
        status = _stp_to_status(stp)
        stats = ev.get("stats", {})
        home_score, away_score = _score_pair(stats, "g")
        sc_raw = str(ev.get("sc", ""))
        baseball_state = _parse_baseball_state(sc_raw, status)
        odds = _parse_odds_ws(ev.get("odds", []), event_id)
        home_name = ev.get("t1", {}).get("n", "")
        away_name = ev.get("t2", {}).get("n", "")
        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=ev.get("ctry_name", ""),
                sport="baseball",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=str(ev.get("sc", "")),
                seconds_remaining=None,
                event_name=f"{home_name} vs {away_name}",
                event_start_time=_parse_start_time(ev.get("st")),
                external_ids={"goalserve": event_id},
                raw_status=str(stp),
                observed_at=observed_at,
                baseball_state=baseball_state,
                source_payload={"goalserve_odds": odds.as_dict()},
            )
        )
    return results


# ---------------------------------------------------------------------------
# Tennis
# ---------------------------------------------------------------------------


def _parse_tennis(state_dict: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in state_dict.items():
        stp = ev.get("stp", 0)
        status = _stp_to_status(stp)
        stats = ev.get("stats", {})
        # WS 网球 stats（已对真实消息校准）：T=已赢盘数、S1..S5=各盘局分、
        # POINTS=当前局比分、pc=当前盘号。sc 是状态码（见 states 字典），不是赛段。
        sets_a, sets_b, set_scores, current_set, home_cur, away_cur = _set_based_state(
            stats, ev.get("pc")
        )
        points_h, points_a = _score_pair(stats, "POINTS")
        tennis_state = TennisGameState(
            home_sets_won=sets_a,
            away_sets_won=sets_b,
            current_set=current_set,
            home_current_set_games=home_cur,
            away_current_set_games=away_cur,
            set_scores=set_scores,
            home_point=str(points_h) if points_h is not None else None,
            away_point=str(points_a) if points_a is not None else None,
            serving_side=None,
        )
        odds = _parse_odds_ws(ev.get("odds", []), event_id)
        home_name = ev.get("t1", {}).get("n", "")
        away_name = ev.get("t2", {}).get("n", "")
        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=ev.get("ctry_name", ""),
                sport="tennis",
                participants=(
                    Participant(role="home", name=home_name, score=sets_a, short_name=_tennis_surname(home_name), external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=sets_b, short_name=_tennis_surname(away_name), external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=f"Set {current_set}" if current_set else "",
                seconds_remaining=None,
                event_name=f"{home_name} vs {away_name}",
                event_start_time=_parse_start_time(ev.get("st")),
                external_ids={"goalserve": event_id},
                raw_status=str(ev.get("sc", stp)),
                observed_at=observed_at,
                tennis_state=tennis_state,
                source_payload={"goalserve_odds": odds.as_dict()},
            )
        )
    return results


# ---------------------------------------------------------------------------
# Esports
# ---------------------------------------------------------------------------


def _parse_esports(state_dict: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in state_dict.items():
        stp = ev.get("stp", 0)
        status = _stp_to_status(stp)
        stats = ev.get("stats", {})
        home_maps, away_maps = _score_pair(stats, "g")
        esports_state = EsportsGameState(
            home_maps_won=home_maps or 0,
            away_maps_won=away_maps or 0,
        )
        odds = _parse_odds_ws(ev.get("odds", []), event_id)
        home_name = ev.get("t1", {}).get("n", "")
        away_name = ev.get("t2", {}).get("n", "")
        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=ev.get("ctry_name", ""),
                sport="esports",
                participants=(
                    Participant(role="home", name=home_name, score=home_maps, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_maps, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=str(ev.get("sc", "")),
                seconds_remaining=None,
                event_name=f"{home_name} vs {away_name}",
                event_start_time=_parse_start_time(ev.get("st")),
                external_ids={"goalserve": event_id},
                raw_status=str(stp),
                observed_at=observed_at,
                esports_state=esports_state,
                source_payload={"goalserve_odds": odds.as_dict()},
            )
        )
    return results


# ---------------------------------------------------------------------------
# American Football
# ---------------------------------------------------------------------------


def _parse_amfootball(state_dict: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in state_dict.items():
        stp = ev.get("stp", 0)
        status = _stp_to_status(stp)
        stats = ev.get("stats", {})
        home_score, away_score = _score_pair(stats, "g")
        et = _int_val(ev.get("et"))
        odds = _parse_odds_ws(ev.get("odds", []), event_id)
        home_name = ev.get("t1", {}).get("n", "")
        away_name = ev.get("t2", {}).get("n", "")
        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=ev.get("ctry_name", ""),
                sport="american-football",
                participants=(
                    Participant(role="home", name=home_name, score=home_score, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_score, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=str(ev.get("sc", "")),
                seconds_remaining=_amfootball_seconds_remaining(et, status),
                event_name=f"{home_name} vs {away_name}",
                event_start_time=_parse_start_time(ev.get("st")),
                external_ids={"goalserve": event_id},
                raw_status=str(stp),
                observed_at=observed_at,
                source_payload={"goalserve_odds": odds.as_dict()},
            )
        )
    return results


# ---------------------------------------------------------------------------
# Volleyball
# ---------------------------------------------------------------------------


def _parse_volleyball(state_dict: dict[str, Any], observed_at: datetime) -> list[LiveEvent]:
    results: list[LiveEvent] = []
    for event_id, ev in state_dict.items():
        stp = ev.get("stp", 0)
        status = _stp_to_status(stp)
        stats = ev.get("stats", {})
        # WS 排球 stats（已对真实消息校准）：T=已赢盘数、S1..S5=各盘比分、
        # pc=当前盘号。与网球同构。
        home_sets_n, away_sets_n, set_scores, current_set, home_cur, away_cur = _set_based_state(
            stats, ev.get("pc")
        )
        vball_state = VolleyballGameState(
            home_sets_won=home_sets_n,
            away_sets_won=away_sets_n,
            current_set=current_set,
            home_current_set_points=home_cur,
            away_current_set_points=away_cur,
            set_scores=set_scores,
        )
        odds = _parse_odds_ws(ev.get("odds", []), event_id)
        home_name = ev.get("t1", {}).get("n", "")
        away_name = ev.get("t2", {}).get("n", "")
        results.append(
            LiveEvent(
                source="goalserve",
                source_event_id=event_id,
                kind=LiveEventKind.TEAM_MATCH,
                league=ev.get("ctry_name", ""),
                sport="volleyball",
                participants=(
                    Participant(role="home", name=home_name, score=home_sets_n, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_sets_n, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=f"Set {current_set}" if current_set else "",
                seconds_remaining=None,
                event_name=f"{home_name} vs {away_name}",
                event_start_time=_parse_start_time(ev.get("st")),
                external_ids={"goalserve": event_id},
                raw_status=str(ev.get("sc", stp)),
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


def parse_goalserve_ws_events(
    sport: str,
    state_dict: dict[str, Any],
    *,
    observed_at: datetime | None = None,
) -> list[LiveEvent]:
    """顶层分派：把 GoalserveClient 内存快照转成 LiveEvent 列表。

    state_dict = {event_id: ws_message_dict}，由 GoalserveClient._state[sport] 提供。
    stp=99 的事件已由 GoalserveClient._handle_message 过滤，此处不再检查。
    """
    ts = observed_at or utc_now()
    parser = _SPORT_PARSERS.get(sport.lower())
    if parser is None or not state_dict:
        return []
    return parser(state_dict, ts)
