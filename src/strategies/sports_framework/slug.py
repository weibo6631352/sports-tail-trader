"""体育盘口 slug 解析与结算范围（scope）识别。

只承载"任何体育策略都需要"的通用解析。具体盘口类型（如 tennis set winner /
tennis set games total）的细节解析仍可在策略私有包内扩展，参见
``strategies.current.tail.slug``。
"""

from __future__ import annotations

from .types import (
    SportsMarketScope,
    SportsMarketScopeType,
    SportsMarketSnapshot,
    SportsMarketType,
)


def normalized_market_slug(market: SportsMarketSnapshot) -> str:
    """归一化 market slug 用于子串匹配。"""

    return (market.market_slug or "").strip().lower().replace("_", " ").replace("-", " ")


def is_unsupported_period_total(text: str) -> bool:
    """识别当前没有独立直播字段支撑的分段 totals。"""

    period_markers = (
        "first quarter total",
        "1st quarter total",
        "second quarter total",
        "2nd quarter total",
        "third quarter total",
        "3rd quarter total",
        "fourth quarter total",
        "4th quarter total",
        "first half total",
        "1st half total",
        "second half total",
        "2nd half total",
        "first inning total",
        "1st inning total",
        "first 5 innings total",
        "first five innings total",
    )
    return any(marker in text for marker in period_markers)


def is_tennis_scope_candidate(market: SportsMarketSnapshot) -> bool:
    """通过 slug 关键字判定是否为网球盘口。"""

    text = normalized_market_slug(market)
    return "tennis" in text or "atp" in text or "wta" in text


def _tennis_set_games_total_number(text: str) -> int | None:
    set_markers = (
        (1, ("first set total", "1st set total", "set 1 total")),
        (2, ("second set total", "2nd set total", "set 2 total")),
        (3, ("third set total", "3rd set total", "set 3 total")),
        (4, ("fourth set total", "4th set total", "set 4 total")),
        (5, ("fifth set total", "5th set total", "set 5 total")),
    )
    for set_number, markers in set_markers:
        if any(marker in text for marker in markers):
            return set_number
    return None


def totals_market_scope(market: SportsMarketSnapshot) -> SportsMarketScope:
    """识别 totals 盘口的结算范围。"""

    text = normalized_market_slug(market)
    set_number = _tennis_set_games_total_number(text)
    if set_number is not None:
        return SportsMarketScope(SportsMarketScopeType.TENNIS_SET_GAMES, set_number)
    if is_unsupported_period_total(text):
        return SportsMarketScope(SportsMarketScopeType.UNSUPPORTED_PERIOD)
    if "set total" in text or "set totals" in text or "total sets" in text:
        return SportsMarketScope(SportsMarketScopeType.TENNIS_TOTAL_SETS)
    if "match total" in text or "total games" in text:
        if is_tennis_scope_candidate(market):
            return SportsMarketScope(SportsMarketScopeType.TENNIS_MATCH_GAMES)
        return SportsMarketScope(SportsMarketScopeType.FULL_GAME)
    if is_tennis_scope_candidate(market):
        return SportsMarketScope(SportsMarketScopeType.TENNIS_MATCH_GAMES)
    return SportsMarketScope(SportsMarketScopeType.FULL_GAME)


def market_scope(market: SportsMarketSnapshot) -> SportsMarketScope:
    """返回盘口的结构化结算范围。"""

    if market.scope_type != SportsMarketScopeType.FULL_GAME or market.scope_number is not None:
        return SportsMarketScope(market.scope_type, market.scope_number)
    if market.market_type == SportsMarketType.TOTALS:
        return totals_market_scope(market)
    return SportsMarketScope(SportsMarketScopeType.FULL_GAME)
