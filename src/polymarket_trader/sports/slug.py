"""体育盘口 slug 解析与结算范围（scope）识别。

只承载"任何体育策略都需要"的通用解析。具体盘口类型（如 tennis set winner /
tennis set games total）的细节解析仍可在策略私有包内扩展，参见
``polymarket_trader.quant.tail.slug``。
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
    """识别当前没有独立直播字段支撑的分段 totals。

    Polymarket slug 会出现两类写法：完整词（"first quarter total"）和简写
    （"1q total" / "1h total"），后者来自实际 slug 形如
    ``nba-det-cle-2026-05-11-1h-total-109pt5``。两类都要覆盖，否则简写写法
    会被识别为 FULL_GAME，错误进入 ``market_end_too_far`` 假阳性拒绝
    （实测 NBA 1H total 因 endDate 是整场比赛结束被假阳性拒绝）。
    """

    period_markers = (
        # 完整词
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
        # 简写（NBA / NFL slug 常用）
        "1h total",
        "2h total",
        "1q total",
        "2q total",
        "3q total",
        "4q total",
        "1p total",
        "2p total",
        "3p total",
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


def _has_token(text: str, token: str) -> bool:
    """判断 token 是否作为独立词出现（slug 已把连字符/下划线转成空格）。"""

    return f" {token} " in f" {text} "


def is_basketball_first_half(text: str) -> bool:
    """识别篮球上半场盘口（1H total / 1H spread / 1H moneyline）。

    text 为 normalized_market_slug 输出（连字符已转空格），故 "1h" 是独立 token。
    """

    return _has_token(text, "1h") or "first half" in text or "1st half" in text


def is_basketball_second_half(text: str) -> bool:
    """识别篮球下半场盘口（2H = Q3+Q4）。"""

    return _has_token(text, "2h") or "second half" in text or "2nd half" in text


_QUARTER_MARKERS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, ("1q", "q1", "first quarter", "1st quarter")),
    (2, ("2q", "q2", "second quarter", "2nd quarter")),
    (3, ("3q", "q3", "third quarter", "3rd quarter")),
    (4, ("4q", "q4", "fourth quarter", "4th quarter")),
)


def basketball_quarter_number(text: str) -> int | None:
    """从 slug 识别篮球单节盘口的节号（Q1-Q4），否则返回 None。"""

    for quarter, markers in _QUARTER_MARKERS:
        for marker in markers:
            if " " in marker:
                if marker in text:
                    return quarter
            elif _has_token(text, marker):
                return quarter
    return None


def is_subperiod_market(text: str) -> bool:
    """识别任意运动的分段盘口（节/半场/分节/前 5 局等）。

    用于 MONEYLINE / SPREADS：这些类型此前没有分段 scope 识别，落到
    FULL_GAME 后被通用评估器拒成误导性的 ``missing_*_state``。
    """

    if is_basketball_first_half(text) or is_basketball_second_half(text):
        return True
    if basketball_quarter_number(text) is not None:
        return True
    period_markers = (
        "first period",
        "1st period",
        "second period",
        "2nd period",
        "third period",
        "3rd period",
        "first 5 innings",
        "first five innings",
        "first inning",
        "1st inning",
    )
    if any(marker in text for marker in period_markers):
        return True
    return _has_token(text, "1p") or _has_token(text, "2p") or _has_token(text, "3p") or _has_token(text, "f5")


def market_scope(market: SportsMarketSnapshot) -> SportsMarketScope:
    """返回盘口的结构化结算范围。"""

    if market.scope_type != SportsMarketScopeType.FULL_GAME or market.scope_number is not None:
        return SportsMarketScope(market.scope_type, market.scope_number)
    text = normalized_market_slug(market)
    if is_basketball_first_half(text):
        return SportsMarketScope(SportsMarketScopeType.BASKETBALL_FIRST_HALF)
    # 篮球下半场 / 单节 ML+spread：从分节得分可干净锁定，需识别出 scope
    # 让评估器分派到专属评估器，而非误判为 FULL_GAME。
    if market.market_type in {SportsMarketType.MONEYLINE, SportsMarketType.SPREADS}:
        if is_basketball_second_half(text):
            return SportsMarketScope(SportsMarketScopeType.BASKETBALL_SECOND_HALF)
        quarter = basketball_quarter_number(text)
        if quarter is not None:
            return SportsMarketScope(SportsMarketScopeType.BASKETBALL_QUARTER, quarter)
        # 其它运动的分段 ML/spread（冰球分节、棒球 F5 等）当前无分段模型，
        # 标记为 UNSUPPORTED_SUBPERIOD，由评估器给出精确可审计拒绝原因。
        if is_subperiod_market(text):
            return SportsMarketScope(SportsMarketScopeType.UNSUPPORTED_SUBPERIOD)
    if market.market_type == SportsMarketType.TOTALS:
        return totals_market_scope(market)
    return SportsMarketScope(SportsMarketScopeType.FULL_GAME)
