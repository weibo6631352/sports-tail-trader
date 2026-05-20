"""当前体育扫尾策略的外部直播状态映射。

本模块只处理体育事件与市场的文本匹配、运动类型识别和候选事件预过滤。
不包含策略决策逻辑（尾盘条件、入场阈值等由 trading/tail_bypass.py 承担）。

匹配按 ``LiveEvent.kind`` 分支：team_match 走 home/away 别名匹配，
race 走 leader_driver / top-3 driver / event_name 关键字命中。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
import unicodedata
from typing import Any

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    SportsLiveGameStatus,
)
from polymarket_trader.extension_api.live_state import LiveStateMatch

_GENERIC_ALIAS_TOKENS = {
    "a",
    "an",
    "and",
    "at",
    "basket",
    "bc",
    "bk",
    "cd",
    "club",
    "csd",
    "dep",
    "deportivo",
    "fc",
    "game",
    "hkk",
    "kk",
    "match",
    "market",
    "men",
    "office",
    "sc",
    "shoes",
    "sk",
    "sport",
    "sports",
    "team",
    "the",
    "to",
    "vs",
    "v",
    "women",
}
_TEAM_EVENT_START_TOLERANCE = timedelta(hours=3)
# 网球资格赛/小赛会的页面时间和直播源时间可能跨日重排；跨源 24h 兜底（保留原逻辑）。
_TENNIS_EVENT_START_TOLERANCE = timedelta(hours=24)

# 可调校准常数；随匹配质量数据积累后再调整。
# confidence 归一化基准：score 来自 alias 长度累加（典型对阵 5–25）。
# 30 作为分母把"双方都中长名 + 完整别名"映射到约 1.0；溢出 clamp 到 1.0。
_CONFIDENCE_SCORE_SCALE = 30.0
# RACE kind 匹配天然弱于 team-pair（leader 文本短、易撞名）→ 0.7 折扣压低优先级。
_RACE_CONFIDENCE_DISCOUNT = 0.7
# RACE 文本命中权重；每命中一个 leader / driver / event 关键字加分，最终归一到 confidence。
_RACE_LEADER_DRIVER_SCORE = 12  # leader driver 关键字命中
_RACE_TOP3_DRIVER_SCORE = 6     # top-3 driver（非 leader）命中
_RACE_EVENT_NAME_SCORE = 4      # event_name 关键字命中
# RACE top-N driver 阈值（position <= 此值参与扫描）
_RACE_TOP_POSITION_THRESHOLD = 3
# token 级单词最小长度，防止短词撞名（单 token 匹配时强制 >= 此值）
_PHRASE_TOKEN_MIN_LENGTH = 3


@dataclass(frozen=True, slots=True)
class LiveMarketMatch:
    """外部 LiveEvent 与 Polymarket market 的文本匹配结果。

    ``primary_source`` / ``contributing_sources`` / ``confidence`` 把"该 market
    实际匹配到的源"作为一等可观测信息（缺口 1：per-market 源选择可观测性）。
    """

    market: Market
    event: LiveEvent
    score: int
    matched_home_alias: str
    matched_away_alias: str
    kind: LiveEventKind = LiveEventKind.TEAM_MATCH

    @property
    def primary_source(self) -> str:
        return self.event.source

    @property
    def contributing_sources(self) -> tuple[str, ...]:
        return tuple(self.event.contributing_sources)

    @property
    def confidence(self) -> float:
        # 把 score 归一化到 [0, 1] 区间，再按 kind 折扣。score 主要来自 alias 长度，
        # 不同 sport 之间难以严格统一，但 [0, 1] 化后足够 admin / 校准 harness 比较。
        base = min(1.0, max(0.0, self.score / _CONFIDENCE_SCORE_SCALE))
        if self.kind == LiveEventKind.RACE:
            return base * _RACE_CONFIDENCE_DISCOUNT
        return base

    def metadata(self) -> dict[str, Any]:
        """返回当前策略读取的入场 metadata。"""

        return {
            "live_game": live_event_metadata(self.event),
            "live_match": {
                "source": self.event.source,
                "source_event_id": self.event.source_event_id,
                "score": self.score,
                "matched_home_alias": self.matched_home_alias,
                "matched_away_alias": self.matched_away_alias,
                "kind": self.kind.value,
                "primary_source": self.primary_source,
                "contributing_sources": list(self.contributing_sources),
                "confidence": self.confidence,
            },
            "goalserve_moneyline": _extract_goalserve_moneyline(self.event),
            "goalserve_spread": _extract_goalserve_spread(self.event),
            "goalserve_totals": _extract_goalserve_totals(self.event),
            "goalserve_halftime": _extract_goalserve_halftime_odds(self.event),
        }


def live_event_metadata(event: LiveEvent) -> dict[str, Any]:
    """把通用直播事件转换成体育扫尾策略的稳定 metadata。

    team_match：导出 home/away 分数；
    race：导出 race_state（leader / laps / 状态旗）+ top-3 drivers；
    所有源融合后产生的 source_conflicts 作为一等字段（不再读 source_payload dict）。
    """

    home = event.home
    away = event.away
    return {
        "league": event.league,
        "sport": event.sport,
        "kind": event.kind.value,
        "home_name": home.name if home else None,
        "away_name": away.name if away else None,
        "home_score": (home.score or 0) if home else 0,
        "away_score": (away.score or 0) if away else 0,
        "period": event.period,
        "seconds_remaining": event.seconds_remaining,
        "status": event.status.value,
        "observed_at": None if event.observed_at is None else event.observed_at.isoformat(),
        "event_start_time": (
            None if event.event_start_time is None else event.event_start_time.isoformat()
        ),
        "event_name": event.event_name,
        "source": event.source,
        "source_event_id": event.source_event_id,
        "raw_status": event.raw_status,
        "source_conflicts": [
            {
                "field": c.field,
                "winner_source": c.winner_source,
                "winner_value": c.winner_value,
                "loser_source": c.loser_source,
                "loser_value": c.loser_value,
                "decided_by": c.decided_by,
            }
            for c in event.source_conflicts
        ],
        "contributing_sources": list(event.contributing_sources),
        "baseball_state": None if event.baseball_state is None else {
            "current_inning": event.baseball_state.current_inning,
            "inning_half": event.baseball_state.inning_half,
            "outs": event.baseball_state.outs,
            "offense_team": event.baseball_state.offense_team,
            "defense_team": event.baseball_state.defense_team,
            "occupied_bases": event.baseball_state.occupied_bases,
        },
        "tennis_state": _tennis_state_metadata(event.tennis_state),
        "soccer_state": None if event.soccer_state is None else {
            "period": event.soccer_state.period,
            "clock_minutes": event.soccer_state.clock_minutes,
            "added_minutes": event.soccer_state.added_minutes,
            "home_red_cards": event.soccer_state.home_red_cards,
            "away_red_cards": event.soccer_state.away_red_cards,
        },
        "esports_state": None if event.esports_state is None else {
            "best_of": event.esports_state.best_of,
            "current_map_index": event.esports_state.current_map_index,
            "home_maps_won": event.esports_state.home_maps_won,
            "away_maps_won": event.esports_state.away_maps_won,
            "home_current_map_score": event.esports_state.home_current_map_score,
            "away_current_map_score": event.esports_state.away_current_map_score,
        },
        "cricket_state": None if event.cricket_state is None else {
            "current_innings": event.cricket_state.current_innings,
            "batting_side": event.cricket_state.batting_side,
            "runs": event.cricket_state.runs,
            "wickets": event.cricket_state.wickets,
            "overs_completed": event.cricket_state.overs_completed,
            "target": event.cricket_state.target,
            "required_runs": event.cricket_state.required_runs,
            "required_balls": event.cricket_state.required_balls,
        },
        "race_state": None if event.race_state is None else {
            "leader_driver": event.race_state.leader_driver,
            "leader_team": event.race_state.leader_team,
            "laps_completed": event.race_state.laps_completed,
            "total_laps": event.race_state.total_laps,
            "status_flag": event.race_state.status_flag,
            "drivers": [
                {
                    "name": d.name,
                    "position": d.position,
                    "team": d.team,
                }
                for d in event.drivers
            ],
        },
    }


def _is_moneyline_market_name(name: str) -> bool:
    """匹配 Goalserve Money Line 盘口名称，兼容带空格和不带空格两种写法。"""
    n = name.lower()
    return "money line" in n or "moneyline" in n


def _goalserve_markets(event: LiveEvent) -> list[dict[str, Any]] | None:
    """从 source_payload 取 Goalserve odds markets 列表，供各盘口提取函数共用。"""
    payload = event.source_payload
    if not payload:
        return None
    odds_dict = payload.get("goalserve_odds")
    if not isinstance(odds_dict, dict):
        return None
    markets = odds_dict.get("markets")
    if not isinstance(markets, list) or not markets:
        return None
    return markets


def _extract_goalserve_moneyline(event: LiveEvent) -> dict[str, Any] | None:
    """从 event.source_payload 提取 Goalserve Money Line 赔率，供 entry 定价用。

    返回 None 表示本事件没有 Goalserve odds 或 Money Line 盘口被暂停/不存在。
    调用侧应视 None 为"无 Goalserve 定价信号"，不阻塞入场判断。
    """
    markets = _goalserve_markets(event)
    if markets is None:
        return None
    # 只取名称含 "money line" 或 "moneyline" 且未暂停的盘口——不用非 Money Line
    # 盘口作为 fallback，因为让分/大小分盘口的隐含概率语义不同，用错来源会导致
    # 交叉验证误判。同时支持带空格 ("money line") 和不带空格 ("moneyline") 两种写法。
    ml_market = next(
        (
            m for m in markets
            if _is_moneyline_market_name(m.get("name", "")) and not m.get("suspended")
        ),
        None,
    )
    if ml_market is None:
        return None
    outcomes = ml_market.get("outcomes", [])
    home_outcome = next((o for o in outcomes if o.get("name", "").lower() in ("home", "1")), None)
    away_outcome = next((o for o in outcomes if o.get("name", "").lower() in ("away", "2")), None)
    if home_outcome is None or away_outcome is None:
        return None
    try:
        home_eu = float(home_outcome.get("value_eu", 0) or 0)
        away_eu = float(away_outcome.get("value_eu", 0) or 0)
        if home_eu <= 0 or away_eu <= 0:
            return None
        home_implied = round(1.0 / home_eu, 6)
        away_implied = round(1.0 / away_eu, 6)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return {
        "market_name": ml_market.get("name"),
        "home_eu": home_eu,
        "away_eu": away_eu,
        "home_implied_prob": home_implied,
        "away_implied_prob": away_implied,
        "suspended": bool(ml_market.get("suspended")),
        "home_suspended": bool(home_outcome.get("suspended")),
        "away_suspended": bool(away_outcome.get("suspended")),
    }


def _extract_goalserve_spread(event: LiveEvent) -> dict[str, Any] | None:
    """从 Goalserve odds 提取让分盘（Spread/Handicap）数据，供策略方向确认用。

    让分盘口可验证"哪支球队被看好赢得更多分"，用于与 Moneyline 交叉确认方向性。
    返回 None 表示无让分盘口数据，不影响主入场判断。
    """
    markets = _goalserve_markets(event)
    if markets is None:
        return None
    spread_market = next(
        (
            m for m in markets
            if (
                ("spread" in m.get("name", "").lower() or "handicap" in m.get("name", "").lower())
                and "2nd half" not in m.get("name", "").lower()
                and "quarter" not in m.get("name", "").lower()
                and not m.get("suspended")
            )
        ),
        None,
    )
    if spread_market is None:
        return None
    outcomes = spread_market.get("outcomes", [])
    home_outcome = next((o for o in outcomes if o.get("name", "").lower() in ("home", "1")), None)
    away_outcome = next((o for o in outcomes if o.get("name", "").lower() in ("away", "2")), None)
    if home_outcome is None or away_outcome is None:
        return None
    try:
        home_eu = float(home_outcome.get("value_eu", 0) or 0)
        away_eu = float(away_outcome.get("value_eu", 0) or 0)
        if home_eu <= 0 or away_eu <= 0:
            return None
        home_implied = round(1.0 / home_eu, 6)
        away_implied = round(1.0 / away_eu, 6)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return {
        "market_name": spread_market.get("name"),
        "home_handicap": home_outcome.get("handicap"),
        "away_handicap": away_outcome.get("handicap"),
        "home_eu": home_eu,
        "away_eu": away_eu,
        "home_implied_prob": home_implied,
        "away_implied_prob": away_implied,
        "suspended": bool(spread_market.get("suspended")),
        "home_suspended": bool(home_outcome.get("suspended")),
        "away_suspended": bool(away_outcome.get("suspended")),
    }


def _extract_goalserve_totals(event: LiveEvent) -> dict[str, Any] | None:
    """从 Goalserve odds 提取大小分盘（Totals/Over-Under）数据，供策略进攻节奏判断用。

    大小分盘反映书商对比赛总得分走向的预判，篮球/冰球/棒球场景下可作辅助方向信号。
    返回 None 表示无大小分数据，不影响主入场判断。
    """
    markets = _goalserve_markets(event)
    if markets is None:
        return None
    totals_market = next(
        (
            m for m in markets
            if (
                (
                    "over" in m.get("name", "").lower()
                    or "total" in m.get("name", "").lower()
                    or "under" in m.get("name", "").lower()
                )
                and "2nd half" not in m.get("name", "").lower()
                and "quarter" not in m.get("name", "").lower()
                and not m.get("suspended")
            )
        ),
        None,
    )
    if totals_market is None:
        return None
    outcomes = totals_market.get("outcomes", [])
    over_outcome = next((o for o in outcomes if "over" in o.get("name", "").lower()), None)
    under_outcome = next((o for o in outcomes if "under" in o.get("name", "").lower()), None)
    if over_outcome is None or under_outcome is None:
        return None
    try:
        over_eu = float(over_outcome.get("value_eu", 0) or 0)
        under_eu = float(under_outcome.get("value_eu", 0) or 0)
        if over_eu <= 0 or under_eu <= 0:
            return None
        over_implied = round(1.0 / over_eu, 6)
        under_implied = round(1.0 / under_eu, 6)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return {
        "market_name": totals_market.get("name"),
        "total_line": over_outcome.get("handicap") or under_outcome.get("handicap"),
        "over_eu": over_eu,
        "under_eu": under_eu,
        "over_implied_prob": over_implied,
        "under_implied_prob": under_implied,
        "suspended": bool(totals_market.get("suspended")),
        "over_suspended": bool(over_outcome.get("suspended")),
        "under_suspended": bool(under_outcome.get("suspended")),
    }


def _extract_goalserve_halftime_odds(event: LiveEvent) -> dict[str, Any] | None:
    """从 Goalserve odds 提取半场/第二节盘口，作为领先方走势确认信号。

    半场盘口（2nd Half / 2nd Quarter 等）的赔率变化反映领先方能否保持优势，
    可用于验证"当前领先程度是否足以支撑扫尾买入"。
    返回 None 表示无半场盘口，不影响主判断。
    """
    markets = _goalserve_markets(event)
    if markets is None:
        return None
    # 优先取 2nd Half Money Line，其次任意含 "half" 且 Money Line 的盘口
    half_market = next(
        (
            m for m in markets
            if (
                ("2nd half" in m.get("name", "").lower() or "half" in m.get("name", "").lower())
                and _is_moneyline_market_name(m.get("name", ""))
                and not m.get("suspended")
            )
        ),
        None,
    )
    if half_market is None:
        return None
    outcomes = half_market.get("outcomes", [])
    home_outcome = next((o for o in outcomes if o.get("name", "").lower() in ("home", "1")), None)
    away_outcome = next((o for o in outcomes if o.get("name", "").lower() in ("away", "2")), None)
    if home_outcome is None or away_outcome is None:
        return None
    try:
        home_eu = float(home_outcome.get("value_eu", 0) or 0)
        away_eu = float(away_outcome.get("value_eu", 0) or 0)
        if home_eu <= 0 or away_eu <= 0:
            return None
        home_implied = round(1.0 / home_eu, 6)
        away_implied = round(1.0 / away_eu, 6)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return {
        "market_name": half_market.get("name"),
        "home_eu": home_eu,
        "away_eu": away_eu,
        "home_implied_prob": home_implied,
        "away_implied_prob": away_implied,
        "suspended": bool(half_market.get("suspended")),
        "home_suspended": bool(home_outcome.get("suspended")),
        "away_suspended": bool(away_outcome.get("suspended")),
    }


def match_live_event(market: Market, event: LiveEvent) -> LiveMarketMatch | None:
    """按 ``event.kind`` 分支，匹配一个外部 LiveEvent 到一个本地 market。

    这里不判断是否值得交易，只解决"这个事件属于哪个 market"的业务语义。
    """

    market_text = _market_text(market)
    return _match_live_event_from_market_text(market, event, market_text)


def _match_live_event_from_market_text(
    market: Market,
    event: LiveEvent,
    market_text: str,
) -> LiveMarketMatch | None:
    if event.kind == LiveEventKind.TEAM_MATCH:
        return _match_team_event(market, event, market_text)
    if event.kind == LiveEventKind.RACE:
        return _match_race_event(market, event, market_text)
    return None


def _match_team_event(
    market: Market,
    event: LiveEvent,
    market_text: str,
) -> LiveMarketMatch | None:
    home = event.home
    away = event.away
    if home is None or away is None:
        return None
    market_start = _market_event_start_time(market)
    event_start = _event_start_time(event)
    precise_start_matched = False
    if (
        market_start is not None
        and event_start is not None
        and not _allows_event_start_time_match(event, market_start, event_start)
    ):
        return None
    if market_start is not None and event_start is not None:
        precise_start_matched = True
    market_date = _market_event_date(market_text)
    event_date = _event_date(event)
    if (
        not precise_start_matched
        and market_date is not None
        and event_date is not None
        and market_date != event_date
        and not _allows_adjacent_event_date(event, market_date, event_date)
    ):
        return None
    market_tokens = set(market_text.split())
    home_alias = _best_alias(market_text, market_tokens, home.match_aliases())
    away_alias = _best_alias(market_text, market_tokens, away.match_aliases())
    if home_alias is None or away_alias is None:
        return None
    home_score = _alias_score(home_alias)
    away_score = _alias_score(away_alias)
    return LiveMarketMatch(
        market=market,
        event=event,
        score=home_score + away_score,
        matched_home_alias=home_alias,
        matched_away_alias=away_alias,
        kind=LiveEventKind.TEAM_MATCH,
    )


def _match_race_event(
    market: Market,
    event: LiveEvent,
    market_text: str,
) -> LiveMarketMatch | None:
    """race kind 文本匹配：扫 market 文本里是否提到 leader / top-3 driver / event_name。

    赛车 market 形态多样（"Will <driver> win the race?"、"<event> winner"
    yes/no prop 等），所以这里用关键字命中而不是 home/away pair。命中后用
    较低 score（× _RACE_CONFIDENCE_DISCOUNT）让 best_live_match 不压制 team-pair
    匹配——两类比赛使用相同的 confidence 字段进入排序。
    """

    race_state = event.race_state
    drivers = event.drivers
    market_tokens = set(market_text.split())
    score = 0
    matched_driver: str | None = None
    matched_event_alias: str | None = None
    leader = (race_state.leader_driver if race_state else None) or None
    if leader and _phrase_matches(leader, market_text, market_tokens):
        score += _RACE_LEADER_DRIVER_SCORE
        matched_driver = leader
    if drivers:
        # 给 leader 之后的 top 候选也做一次扫描，命中其中之一即可。
        top_drivers = [d.name for d in drivers if d.position is not None and d.position <= _RACE_TOP_POSITION_THRESHOLD]
        for driver_name in top_drivers:
            if driver_name == leader:
                continue
            if _phrase_matches(driver_name, market_text, market_tokens):
                score += _RACE_TOP3_DRIVER_SCORE
                if matched_driver is None:
                    matched_driver = driver_name
                break
    if event.event_name and _phrase_matches(event.event_name, market_text, market_tokens):
        score += _RACE_EVENT_NAME_SCORE
        matched_event_alias = event.event_name
    if score <= 0:
        return None
    return LiveMarketMatch(
        market=market,
        event=event,
        score=score,
        matched_home_alias=matched_driver or "",
        matched_away_alias=matched_event_alias or "",
        kind=LiveEventKind.RACE,
    )


def _phrase_matches(phrase: str, market_text: str, market_tokens: set[str]) -> bool:
    """车手名或赛事名命中文本——保守起见走 token 级匹配，不让短词撞名。"""

    normalized = _normalize_text(phrase)
    tokens = [token for token in normalized.split() if token not in _GENERIC_ALIAS_TOKENS]
    if not tokens:
        return False
    if len(tokens) == 1:
        return tokens[0] in market_tokens and len(tokens[0]) >= _PHRASE_TOKEN_MIN_LENGTH
    phrase_text = " ".join(tokens)
    return f" {phrase_text} " in f" {market_text} " or all(t in market_tokens for t in tokens)


def best_live_match(
    market: Market,
    events: tuple[LiveEvent, ...],
) -> LiveMarketMatch | None:
    """返回 market 在当前事件集合中的最高置信匹配。"""

    market_text = _market_text(market)
    matches = [
        match
        for event in events
        if (match := _match_live_event_from_market_text(market, event, market_text)) is not None
    ]
    if not matches:
        return None
    # 按 confidence 排序：team-pair 优先，但高 confidence 的 race 也能击败低 confidence team。
    return max(matches, key=lambda item: (item.confidence, item.score))


def build_live_state_match(
    market: Market,
    events: tuple[LiveEvent, ...],
    *,
    market_end_horizon_seconds: int,
    bypass_resolver: "callable | None" = None,
) -> LiveStateMatch | None:
    """框架 hook ``match_live_state`` 的策略侧实现：返回强类型 LiveStateMatch。"""

    match = best_live_match(market, events)
    if match is None:
        return None
    matched_market = match.market
    event = match.event
    signal_allowed, signal_reason = entry_signal_gate(
        matched_market,
        event,
        market_end_horizon_seconds=market_end_horizon_seconds,
    )
    if not signal_allowed and signal_reason == "market_end_too_far" and bypass_resolver is not None:
        bypass = bypass_resolver(matched_market, event)
        if bypass is not None:
            signal_allowed = True
            signal_reason = bypass
    payload = match.metadata()
    return LiveStateMatch(
        market=matched_market,
        event=event,
        signal_allowed=signal_allowed,
        signal_reason=signal_reason,
        phase=event.status.value,
        primary_source=match.primary_source,
        contributing_sources=match.contributing_sources,
        confidence=match.confidence,
        payload=payload,
    )


def entry_signal_gate(
    market: Market,
    event: LiveEvent,
    *,
    market_end_horizon_seconds: int,
) -> tuple[bool, str]:
    """判断直播匹配是否应触发 P0 入场信号。

    metadata 写入用于候选展示和复盘；P0 entry signal 只给真正进入扫尾观察窗、
    或已经结束但 Polymarket 尚未封盘的 market，避免远期 live 匹配挤压交易队列。
    """

    if event.status == SportsLiveGameStatus.ENDED:
        return True, "ended_not_closed"
    if event.status != SportsLiveGameStatus.LIVE:
        return False, f"sports_live_state_{event.status.value}"
    if market.end_date is None or market_end_horizon_seconds <= 0:
        return True, "market_end_unknown"
    current_time = _ensure_utc(event.observed_at) or datetime.now(timezone.utc)
    market_end = _ensure_utc(market.end_date)
    if market_end is None:
        return True, "market_end_unknown"
    seconds_until_end = (market_end - current_time).total_seconds()
    if seconds_until_end > market_end_horizon_seconds:
        return False, "market_end_too_far"
    return True, "within_tail_window"


def _market_text(market: Market) -> str:
    return _normalize_text(
        " ".join(
            part
            for part in (
                market.market_question,
                market.market_name,
                market.market_slug,
                market.event_title,
                market.event_slug,
                market.category,
                " ".join(market.tags),
                " ".join(outcome.outcome for outcome in market.outcomes),
            )
            if part
        )
    )


def _tennis_state_metadata(state: Any) -> dict[str, Any] | None:
    """把强类型 TennisGameState 投影成策略稳定 metadata。"""

    if state is None:
        return None
    return {
        "home_sets_won": state.home_sets_won,
        "away_sets_won": state.away_sets_won,
        "current_set": state.current_set,
        "home_current_set_games": state.home_current_set_games,
        "away_current_set_games": state.away_current_set_games,
        "home_total_games": state.home_total_games,
        "away_total_games": state.away_total_games,
        "total_games": state.total_games,
        "set_scores": state.set_scores,
        "home_point": state.home_point,
        "away_point": state.away_point,
        "first_to_serve": state.first_to_serve,
        "serving_side": state.serving_side,
    }


def _market_event_date(market_text: str) -> date | None:
    match = re.search(r"(?<!\d)(20\d{2})\s+([01]\d)\s+([0-3]\d)(?!\d)", market_text)
    if match is None:
        return None
    return _date_from_parts(match.group(1), match.group(2), match.group(3))


def _market_event_start_time(market: Market) -> datetime | None:
    """返回 Polymarket 标注的比赛真实开赛时间，而不是 resolution/endDate。"""

    return _ensure_utc(market.game_start_time)


def _event_start_time(event: LiveEvent) -> datetime | None:
    """开赛时间优先读 event.event_start_time 一等字段；缺失时回退 source_payload。

    一等字段是新模型主路径；source_payload 的 fallback 只在历史快照或外部源
    未填一等字段时短暂启用——所有 client 都应主动写一等字段。
    """

    if event.event_start_time is not None:
        return _ensure_utc(event.event_start_time)
    for key in (
        "start_timestamp",
        "start_time_utc",
        "game_time_utc",
        "game_date",
        "date",
    ):
        parsed = _parse_event_datetime_value(event.source_payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _event_date(event: LiveEvent) -> date | None:
    start = _event_start_time(event)
    if start is not None:
        return start.date()
    for key in (
        "start_time_utc",
        "game_time_utc",
        "game_date",
        "official_date",
        "start_timestamp",
    ):
        parsed = _parse_event_date_value(event.source_payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _allows_event_start_time_match(
    event: LiveEvent,
    market_start: datetime,
    event_start: datetime,
) -> bool:
    """用精确开赛时间防止同队多场比赛串场。

    团队联赛常有同队连续多日比赛，不能只靠队名或日期匹配。网球允许较宽的
    赛程漂移窗口，因为资格赛/小赛会的页面时间和直播源时间可能被重排。
    """

    sport = str(event.sport or event.source_payload.get("sport") or "").strip().lower()
    tolerance = (
        _TENNIS_EVENT_START_TOLERANCE
        if sport == "tennis"
        else _TEAM_EVENT_START_TOLERANCE
    )
    return abs(market_start - event_start) <= tolerance


def _allows_adjacent_event_date(event: LiveEvent, market_date: date, event_date: date) -> bool:
    """处理网球跨时区开赛日期。

    Polymarket 网球 slug 常按页面本地日期命名，SofaScore 使用 UTC 开赛时间；
    同一场可能相差一天。团队联赛不使用该宽松规则，避免 MLB/NBA 同队多日赛串场。
    """

    sport = str(event.sport or event.source_payload.get("sport") or "").strip().lower()
    if sport != "tennis":
        return False
    return abs((market_date - event_date).days) <= 1


def _parse_event_datetime_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(_timestamp_seconds(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        try:
            return datetime.fromtimestamp(_timestamp_seconds(float(text)), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    # 纯日期只能用于日期兜底，不能伪装成精确开赛时间参与硬匹配。
    if re.fullmatch(r"20\d{2}-[01]\d-[0-3]\d", text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_utc(parsed)


def _parse_event_date_value(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(?<!\d)(20\d{2})-([01]\d)-([0-3]\d)(?!\d)", text)
    if match is not None:
        return _date_from_parts(match.group(1), match.group(2), match.group(3))
    match = re.search(r"(?<!\d)(20\d{2})([01]\d)([0-3]\d)(?!\d)", text)
    if match is not None:
        return _date_from_parts(match.group(1), match.group(2), match.group(3))
    return None


def _timestamp_seconds(value: int | float) -> float:
    timestamp = float(value)
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    return timestamp


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _date_from_parts(year: str, month: str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _best_alias(
    market_text: str,
    market_tokens: set[str],
    aliases: tuple[str, ...],
) -> str | None:
    candidates: list[tuple[int, str]] = []
    market_compact = _compact_text(market_text)
    for alias in aliases:
        seen_variants: set[str] = set()
        for normalized in _alias_text_variants(alias):
            if not normalized or normalized in seen_variants:
                continue
            seen_variants.add(normalized)
            tokens = tuple(token for token in normalized.split() if token not in _GENERIC_ALIAS_TOKENS)
            if not tokens:
                continue
            if len(tokens) == 1:
                token = tokens[0]
                if len(token) < _PHRASE_TOKEN_MIN_LENGTH and token not in market_tokens:
                    continue
                if token in market_tokens:
                    candidates.append((_alias_score(token), alias))
                continue
            phrase = " ".join(tokens)
            if f" {phrase} " in f" {market_text} ":
                candidates.append((_alias_score(phrase), alias))
                continue
            if all(token in market_tokens for token in tokens):
                candidates.append((_alias_score(phrase) - 1, alias))
                continue
            if _compact_alias_matches_market(tokens, market_compact):
                candidates.append((_alias_score(phrase) - 2, alias))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _alias_score(alias: str) -> int:
    normalized = _normalize_text(alias)
    tokens = [token for token in normalized.split() if token not in _GENERIC_ALIAS_TOKENS]
    return sum(max(1, len(token)) for token in tokens)


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = _fold_ascii(value.lower().replace("&", " and "))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _fold_ascii(value: str) -> str:
    """去除直播源球队名中的重音符号，保持跨来源队名匹配稳定。"""

    normalized = unicodedata.normalize("NFKD", value)
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _alias_text_variants(alias: str) -> tuple[str, ...]:
    """返回外部队名常见语言/拼写变体，用于跨来源匹配。"""

    normalized = _normalize_text(alias)
    if not normalized:
        return ()
    variants = [normalized]
    replaced = re.sub(r"\bolympique\b", "olympic", normalized)
    if replaced != normalized:
        variants.append(replaced)
    translated = re.sub(r"\bthree towns\b", "san zhen", normalized)
    translated = re.sub(r"\btiger\b", "hu", translated)
    if translated != normalized:
        variants.append(translated)
    moroccan = re.sub(r"\bel jadidi\b", "el jadida", normalized)
    moroccan = re.sub(r"\brenaissance zemamra\b", "rca zemamra", moroccan)
    if moroccan != normalized:
        variants.append(moroccan)
    german_basketball = re.sub(r"\bfraport skyliners frankfurt\b", "fraport skyliners", normalized)
    if german_basketball != normalized:
        variants.append(german_basketball)
    russian = re.sub(r"\bmoscow\b", "moskva", normalized)
    russian = re.sub(r"\bdynamo\b", "dinamo", russian)
    if russian != normalized:
        variants.append(russian)
    tahiti = re.sub(r"\btahiti\b", "french polynesia", normalized)
    if tahiti != normalized:
        variants.append(tahiti)
    return tuple(variants)


def _compact_alias_matches_market(tokens: tuple[str, ...], market_compact: str) -> bool:
    if len(tokens) < 2:
        return False
    compact_alias = "".join(tokens)
    if len(compact_alias) < 8:
        return False
    return compact_alias in market_compact


def _compact_text(value: str) -> str:
    return value.replace(" ", "")


# ---------------------------------------------------------------------------
# 运动类型识别 + 候选直播事件过滤
# ---------------------------------------------------------------------------

def _normalized_market_text_for_sport(market: Market) -> str:
    """归一化 market 所有文本字段，用于运动类型关键字匹配。"""
    return " ".join(
        re.sub(
            r"[^a-z0-9]+",
            " ",
            " ".join(
                part
                for part in (
                    market.market_question,
                    market.market_name,
                    market.market_slug,
                    market.event_title,
                    market.event_slug,
                    market.category,
                    " ".join(market.tags),
                    " ".join(outcome.outcome for outcome in market.outcomes),
                )
                if part
            ).lower(),
        ).split()
    )


def _market_sport_codes(market: Market) -> set[str]:
    text = _normalized_market_text_for_sport(market)
    mapping = (
        ("table-tennis", ("table tennis", "table-tennis", "wtt", "world team championships")),
        ("baseball", ("mlb", "kbo", "baseball")),
        ("tennis", ("atp", "wta")),
        ("basketball", ("nba", "wnba", "ncaamb", "ncaawb", "basketball")),
        ("ice-hockey", ("nhl", "ahl", "hockey", "ice hockey")),
        ("american-football", ("nfl", "ncaaf", "american football")),
        ("football", ("soccer", "football", "mls", "nwsl", "epl")),
        ("volleyball", ("volleyball",)),
        ("cricket", ("cricket", "ipl", "t20", "test match", "odi")),
        ("rugby", ("rugby", "six nations", "rugby union", "rugby league")),
        ("handball", ("handball",)),
        ("mma", ("mma", "ufc", "bellator", "mixed martial arts")),
        ("boxing", ("boxing",)),
        ("golf", ("golf", "pga tour", "masters", "open championship", "ryder cup", "lpga")),
        ("horse-racing", ("horse racing", "cheltenham", "kentucky derby", "grand national", "horse race")),
        ("formula1", ("formula 1", "formula1", "f1", "grand prix", "monaco gp")),
        ("motogp", ("motogp", "moto gp")),
    )
    return {sport for sport, tokens in mapping if any(f" {token} " in f" {text} " for token in tokens)}


def _event_sport_code(event: LiveEvent) -> str | None:
    sport = str(event.sport or event.source_payload.get("sport") or "").strip().lower()
    if sport:
        return _normalize_sport_code(sport)
    source = str(event.source or "").strip().lower()
    league = str(event.league or "").strip().lower()
    text = f"{source} {league}"
    if "mlb" in text:
        return "baseball"
    if "nba" in text or "basketball" in text:
        return "basketball"
    if "nhl" in text or "hockey" in text:
        return "ice-hockey"
    if "table-tennis" in text or "table tennis" in text or "wtt" in text:
        return "table-tennis"
    if "tennis" in text or "atp" in text or "wta" in text:
        return "tennis"
    if "football" in text or "soccer" in text:
        return "football"
    if "volleyball" in text:
        return "volleyball"
    if "cricket" in text:
        return "cricket"
    if "rugby" in text:
        return "rugby"
    if "handball" in text:
        return "handball"
    if "mma" in text or "ufc" in text:
        return "mma"
    if "boxing" in text:
        return "boxing"
    if "golf" in text:
        return "golf"
    if "horse" in text and "racing" in text:
        return "horse-racing"
    if "formula" in text or "grand prix" in text:
        return "formula1"
    if "motogp" in text:
        return "motogp"
    return None


def _normalize_sport_code(value: str) -> str:
    normalized = value.replace("_", "-").replace(" ", "-")
    if normalized in {"soccer"}:
        return "football"
    if normalized in {"icehockey"}:
        return "ice-hockey"
    if normalized in {"tabletennis"}:
        return "table-tennis"
    if normalized in {"amfootball", "american-football"}:
        return "american-football"
    if normalized.startswith("golf-"):
        return "golf"
    if normalized.startswith("horse-racing-"):
        return "horse-racing"
    return normalized


def _event_start_is_near_market_start(event: LiveEvent, market_start: datetime) -> bool:
    event_start = _event_start_time(event)
    if event_start is None:
        # event_start_time 缺失时用 observed_at 做保守过滤：
        # market_start 比观测时间晚超过 12h → 该 market 是次日场次，不应匹配已观测赛事。
        # 12h（非 24h）防止"今日赛事"误匹配"次日同联赛 market"（两者相差约 22-23h）。
        observed = _ensure_utc(event.observed_at)
        if observed is not None and market_start > observed + timedelta(hours=12):
            return False
        return True
    tolerance = timedelta(hours=24) if _event_sport_code(event) == "tennis" else timedelta(hours=6)
    return abs(event_start - market_start) <= tolerance


def candidate_live_events_for_market(
    market: Market,
    events: tuple[LiveEvent, ...],
) -> tuple[LiveEvent, ...]:
    """按运动类型和开赛时间预过滤候选直播事件，减少后续全量文本匹配开销。"""
    sport_codes = _market_sport_codes(market)
    market_start = _ensure_utc(market.game_start_time)
    filtered: list[LiveEvent] = []
    for event in events:
        if sport_codes and (event_sport := _event_sport_code(event)) is not None and event_sport not in sport_codes:
            continue
        if market_start is not None and not _event_start_is_near_market_start(event, market_start):
            continue
        filtered.append(event)
    return tuple(filtered)
