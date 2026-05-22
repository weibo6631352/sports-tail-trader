"""扫尾策略私有：网球盘口的细分识别 + 扫尾视角的拒绝原因映射。

通用 slug 解析（``normalized_market_slug`` / ``market_scope`` /
``totals_market_scope`` / ``is_tennis_scope_candidate`` /
``is_unsupported_period_total``）已上提到 ``strategies.sports_framework.slug``；
本模块只承载与扫尾语义绑定的判断。
"""

from __future__ import annotations

from strategies.sports_framework import (
    SportsMarketScope,
    SportsMarketScopeType,
    SportsMarketSnapshot,
    is_tennis_scope_candidate,
    market_scope,
    normalized_market_slug,
)

from .types import TailRejectReason


def _is_tennis_set_winner_market(market: SportsMarketSnapshot) -> bool:
    """识别网球单盘胜者盘口。"""

    text = normalized_market_slug(market)
    return "set winner" in text or "first set winner" in text


def _tennis_set_winner_number(market: SportsMarketSnapshot) -> int | None:
    """从 market slug 识别第几盘胜者盘口。"""

    text = normalized_market_slug(market)
    if "first set winner" in text or "1st set winner" in text or "set 1 winner" in text:
        return 1
    if "second set winner" in text or "2nd set winner" in text or "set 2 winner" in text:
        return 2
    if "third set winner" in text or "3rd set winner" in text or "set 3 winner" in text:
        return 3
    return None


def _is_tennis_set_handicap_market(market: SportsMarketSnapshot) -> bool:
    """识别网球盘分让分（set handicap / set spread）盘口。

    与网球局数让分（games handicap）区分：盘分让分按盘数结算（如 -1.5 盘 =
    需 2-0 取胜），局数让分按整场总局数结算，两者锁定模型完全不同。
    slug 出现 "set handicap" / "set spread" / "sets handicap" 视为盘分让分。
    """

    text = normalized_market_slug(market)
    return "set handicap" in text or "set spread" in text or "sets handicap" in text


def _tennis_total_scope(market: SportsMarketSnapshot) -> SportsMarketScope:
    """返回网球 totals 的结算范围，缺省按整场总局数处理。"""

    scope = market_scope(market)
    if scope.scope_type == SportsMarketScopeType.FULL_GAME:
        return SportsMarketScope(SportsMarketScopeType.TENNIS_MATCH_GAMES)
    return scope


def _market_scope_reject_reason(market: SportsMarketSnapshot) -> TailRejectReason | None:
    """返回当前策略明确不能自动结算的盘口范围拒绝原因。"""

    scope = market_scope(market)
    if scope.scope_type == SportsMarketScopeType.UNSUPPORTED_PERIOD:
        return (
            TailRejectReason.TENNIS_TOTAL_SCOPE_UNSUPPORTED
            if is_tennis_scope_candidate(market)
            else TailRejectReason.UNSUPPORTED_MARKET_SCOPE
        )
    return None
