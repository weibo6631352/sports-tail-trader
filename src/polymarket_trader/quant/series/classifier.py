"""系列赛子类型分类器。

单一来源：series family 的子类型判定只在这里写一份；``outcomes._market_family``
也通过本模块判断 family 归属，避免双口径关键词列表。

deterministic 规则：先匹配更具体的（handicap > total_games > winner），命中即返回。
所有判定基于 ``Market`` 的可见文本（question / event_title / outcomes），不依赖
tags / category——后者噪音大且不稳定。
"""

from __future__ import annotations

from polymarket_trader.domain.market import Market

from polymarket_trader.quant.series.types import SeriesSubType


def classify_series_sub_type(market: Market) -> SeriesSubType:
    """根据 market 文本判定系列赛盘口子类型。

    入参未必是 SERIES family（``outcomes._market_family`` 也用它做 family 归属判定）；
    返回 OTHER 表示文本里没有任何系列赛关键词，调用方据此决定不归到 SERIES。
    """

    text = _candidate_text(market)
    # handicap 必须与 game/series 同现，避免误吞单场 spreads。
    if ("game handicap" in text or "series handicap" in text) or (
        "handicap" in text and ("game" in text or "series" in text)
    ):
        return SeriesSubType.GAME_HANDICAP
    if _matches_total_games(text):
        return SeriesSubType.TOTAL_GAMES
    if _matches_winner(text):
        return SeriesSubType.WINNER
    return SeriesSubType.OTHER


def _matches_total_games(text: str) -> bool:
    # Polymarket 文本变体：games o/u、games ou、total games、games over/under。
    # 单独检测 " total games "（带 padding）避免误吞 "total points"。
    if " total games " in f" {text} ":
        return True
    return any(
        phrase in text
        for phrase in ("games o u", "games ou", "games over", "games under")
    )


def _matches_winner(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "series winner",
            "win series",
            "who will win series",
            "who will win the series",
            "who wins series",
            "who wins the series",
        )
    )


def _candidate_text(market: Market) -> str:
    """汇总并归一化用于 series 分类的文本。

    与 ``outcomes._normalize_text`` 等价（lowercase + 标点压成空格），但本模块
    自带实现避免循环依赖。outcome 文本一起拼进来，让 "Over 5.5 games"
    一类的 outcome label 也能影响判定。
    """

    parts: list[str] = []
    for part in (
        market.market_question,
        market.market_name,
        market.market_slug,
        market.event_title,
    ):
        if part:
            parts.append(part)
    for outcome in market.outcomes:
        if outcome.outcome:
            parts.append(outcome.outcome)
    raw = " ".join(parts)
    return _normalize(raw)


def _normalize(text: str) -> str:
    if not text:
        return ""
    lowered = text.lower().replace("&", " and ")
    out_chars: list[str] = []
    current: list[str] = []
    for ch in lowered:
        if ch.isalnum() or ch in {"+", "-", "."}:
            current.append(ch)
            continue
        if current:
            out_chars.append("".join(current))
            current = []
    if current:
        out_chars.append("".join(current))
    return " ".join(out_chars)


__all__ = ["classify_series_sub_type"]
