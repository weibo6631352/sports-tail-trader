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

# stp 整数 → SportsLiveGameStatus 映射
_STP_STATUS: dict[int, SportsLiveGameStatus] = {
    0: SportsLiveGameStatus.SCHEDULED,
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


def _parse_odds_ws(odds_raw: Any, event_id: str) -> GoalserveOdds:
    """解析 WS odds 列表 → GoalserveOdds。

    WS odds 元素短键格式：id, nm（name）, sp（suspended）, o（outcomes list）
    每个 outcome：nm, v（value_eu）, hc（handicap）, sp（suspended）
    同时兼容长键格式以防服务端变更。
    """
    if not isinstance(odds_raw, list):
        return GoalserveOdds(event_id=event_id, markets=())

    markets: list[GoalserveMarket] = []
    for item in odds_raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("nm") or item.get("name", ""))
        mkt_id = _int_val(item.get("id")) or 0
        mkt_susp_raw = item.get("sp") or item.get("suspend", 0)
        mkt_susp = bool(_int_val(mkt_susp_raw))

        outcomes: list[GoalserveOutcome] = []
        for od in item.get("o", []):
            if not isinstance(od, dict):
                continue
            eu_raw = od.get("v") or od.get("value_eu")
            eu = _dec_val(eu_raw)
            if eu is None or eu <= 0:
                continue
            implied = (Decimal("1") / eu).quantize(Decimal("0.0001"))
            od_susp_raw = od.get("sp") or od.get("suspend", 0)
            outcomes.append(
                GoalserveOutcome(
                    name=str(od.get("nm") or od.get("name", "")),
                    value_eu=eu,
                    implied_prob=implied,
                    handicap=str(od.get("hc") or od.get("handicap", "") or ""),
                    suspended=bool(_int_val(od_susp_raw)),
                )
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


def _et_to_soccer_period(et: int | None) -> str | None:
    """把 elapsed seconds 映射到足球 period 字符串。"""
    if et is None:
        return None
    if et < 2700:
        return "first_half"
    if et < 5400:
        return "second_half"
    return "extra_time"


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
        home_score, away_score = _score_pair(stats, "g")
        home_yellow, away_yellow = _score_pair(stats, "y")
        home_red, away_red = _score_pair(stats, "r")
        et = _int_val(ev.get("et"))
        soccer_state = SoccerGameState(
            period=_et_to_soccer_period(et),
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
                period=_et_to_soccer_period(et) or "",
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
        # WS 网球：stats.g = [home_sets, away_sets]（盘数）
        home_sets, away_sets = _score_pair(stats, "g")
        sets_a = home_sets or 0
        sets_b = away_sets or 0
        tennis_state = TennisGameState(
            home_sets_won=sets_a,
            away_sets_won=sets_b,
            # current_set = 已完成盘数 + 1；当 status 为 LIVE 时为当前打的盘。
            current_set=sets_a + sets_b + 1 if status == SportsLiveGameStatus.LIVE else None,
            home_current_set_games=None,
            away_current_set_games=None,
            set_scores=(),
            home_point=None,
            away_point=None,
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
                    Participant(role="home", name=home_name, score=sets_a, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=sets_b, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=str(ev.get("sc", "")),
                seconds_remaining=None,
                event_name=f"{home_name} vs {away_name}",
                event_start_time=_parse_start_time(ev.get("st")),
                external_ids={"goalserve": event_id},
                raw_status=str(stp),
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
        home_sets, away_sets = _score_pair(stats, "g")
        home_sets_n = home_sets or 0
        away_sets_n = away_sets or 0
        vball_state = VolleyballGameState(
            home_sets_won=home_sets_n,
            away_sets_won=away_sets_n,
            current_set=home_sets_n + away_sets_n + 1 if status == SportsLiveGameStatus.LIVE else None,
            home_current_set_points=None,
            away_current_set_points=None,
            set_scores=(),
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
                    Participant(role="home", name=home_name, score=home_sets, external_ids={"goalserve": event_id}),
                    Participant(role="away", name=away_name, score=away_sets, external_ids={"goalserve": event_id}),
                ),
                status=status,
                period=str(ev.get("sc", "")),
                seconds_remaining=None,
                event_name=f"{home_name} vs {away_name}",
                event_start_time=_parse_start_time(ev.get("st")),
                external_ids={"goalserve": event_id},
                raw_status=str(stp),
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
