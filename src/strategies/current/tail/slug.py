"""体育盘口 slug 解析与结算范围（scope）识别。"""

from __future__ import annotations

from .types import (
    SportsMarketScope,
    SportsMarketScopeType,
    SportsMarketSnapshot,
    SportsMarketType,
    TailRejectReason,
)


def _normalized_market_slug(market: SportsMarketSnapshot) -> str:
    return (market.market_slug or "").strip().lower().replace("_", " ").replace("-", " ")


def _is_tennis_set_winner_market(market: SportsMarketSnapshot) -> bool:
    """识别网球单盘胜者盘口。"""

    text = _normalized_market_slug(market)
    return "set winner" in text or "first set winner" in text


def _tennis_set_winner_number(market: SportsMarketSnapshot) -> int | None:
    """从 market slug 识别第几盘胜者盘口。"""

    text = _normalized_market_slug(market)
    if "first set winner" in text or "1st set winner" in text or "set 1 winner" in text:
        return 1
    if "second set winner" in text or "2nd set winner" in text or "set 2 winner" in text:
        return 2
    if "third set winner" in text or "3rd set winner" in text or "set 3 winner" in text:
        return 3
    return None


def _tennis_set_games_total_number(text: str) -> int | None:
    """识别第一盘/第二盘等单盘总局数盘口。"""

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


def _is_unsupported_period_total(text: str) -> bool:
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


def _is_tennis_scope_candidate(market: SportsMarketSnapshot) -> bool:
    text = _normalized_market_slug(market)
    return "tennis" in text or "atp" in text or "wta" in text


def _totals_market_scope(market: SportsMarketSnapshot) -> SportsMarketScope:
    """识别 totals 盘口的结算范围。"""

    text = _normalized_market_slug(market)
    set_number = _tennis_set_games_total_number(text)
    if set_number is not None:
        return SportsMarketScope(SportsMarketScopeType.TENNIS_SET_GAMES, set_number)
    if _is_unsupported_period_total(text):
        return SportsMarketScope(SportsMarketScopeType.UNSUPPORTED_PERIOD)
    if "set total" in text or "set totals" in text or "total sets" in text:
        return SportsMarketScope(SportsMarketScopeType.TENNIS_TOTAL_SETS)
    if "match total" in text or "total games" in text:
        if _is_tennis_scope_candidate(market):
            return SportsMarketScope(SportsMarketScopeType.TENNIS_MATCH_GAMES)
        return SportsMarketScope(SportsMarketScopeType.FULL_GAME)
    if _is_tennis_scope_candidate(market):
        return SportsMarketScope(SportsMarketScopeType.TENNIS_MATCH_GAMES)
    return SportsMarketScope(SportsMarketScopeType.FULL_GAME)


def _market_scope(market: SportsMarketSnapshot) -> SportsMarketScope:
    """返回盘口的结构化结算范围。"""

    if market.scope_type != SportsMarketScopeType.FULL_GAME or market.scope_number is not None:
        return SportsMarketScope(market.scope_type, market.scope_number)
    if market.market_type == SportsMarketType.TOTALS:
        return _totals_market_scope(market)
    return SportsMarketScope(SportsMarketScopeType.FULL_GAME)


def _tennis_total_scope(market: SportsMarketSnapshot) -> SportsMarketScope:
    """返回网球 totals 的结算范围，缺省按整场总局数处理。"""

    scope = _market_scope(market)
    if scope.scope_type == SportsMarketScopeType.FULL_GAME:
        return SportsMarketScope(SportsMarketScopeType.TENNIS_MATCH_GAMES)
    return scope


def _market_scope_reject_reason(market: SportsMarketSnapshot) -> TailRejectReason | None:
    """返回当前策略明确不能自动结算的盘口范围拒绝原因。"""

    scope = _market_scope(market)
    if scope.scope_type == SportsMarketScopeType.UNSUPPORTED_PERIOD:
        return (
            TailRejectReason.TENNIS_TOTAL_SCOPE_UNSUPPORTED
            if _is_tennis_scope_candidate(market)
            else TailRejectReason.UNSUPPORTED_MARKET_SCOPE
        )
    return None
