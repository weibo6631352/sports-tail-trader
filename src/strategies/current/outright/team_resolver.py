"""把 Polymarket 二元 YES/NO 子市场反向映射回 snapshot 球队 key。

TheOddsAPI ``outrights`` 端点返回 ``{球队名: 概率}``；Polymarket 同一冠军
``event_slug`` 下挂多个二元子市场，每个子市场 outcome 只有 ``"Yes"`` / ``"No"``，
具体指代哪支球队靠 ``market_question`` / ``event_title`` 的文本表达。本模块
负责"snapshot 球队名 ↔ 市场文本"的反向匹配。

匹配规则单一：归一后（小写、`&`→`and`、剔标点、压空白）的 snapshot 球队名以
完整 word boundary 出现在候选文本里，或其 last token 单独出现，即视为命中；
命中集合恰好 1 个返回该 key，0 或 ≥2 视作不可解析返回 None（具体拒绝原因
由 evaluator 区分）。
"""

from __future__ import annotations

import re

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_season import SeasonOddsSnapshot
from strategies.current._shared.team_normalize import normalize_team_name


def resolve_market_team(market: Market, snapshot: SeasonOddsSnapshot) -> str | None:
    """从 snapshot 反向匹配市场文本，找出市场指代的球队 key。

    返回 snapshot 中原始 key（保持原大小写，未归一化），便于直接拿去查
    ``snapshot.fair_probabilities``。歧义（命中 >1）或缺失（命中 0）返回 None。
    """

    text = _candidate_text(market)
    if not text:
        return None
    # deterministic：按 snapshot 自身 key 顺序遍历，保留首次出现顺序。
    matches: list[str] = []
    for key in snapshot.fair_probabilities.keys():
        if _team_matches_text(key, text):
            matches.append(key)
    if len(matches) == 1:
        return matches[0]
    return None


def _candidate_text(market: Market) -> str:
    """拼接 market_question / event_title / event_slug 后归一化。

    event_slug 形如 "2026-nba-championship-winner"，需先把 `-` `_` 转空白
    再交给归一化函数，否则球队 token 会被连字符粘在一起。
    """

    parts: list[str] = []
    if market.market_question:
        parts.append(market.market_question)
    if market.event_title:
        parts.append(market.event_title)
    if market.event_slug:
        parts.append(market.event_slug.replace("-", " ").replace("_", " "))
    joined = " ".join(parts)
    return normalize_team_name(joined)


def _team_matches_text(team_key: str, normalized_text: str) -> bool:
    """归一后判定 team_key 是否在 normalized_text 中以 word boundary 出现。

    先尝试全名（"boston celtics"）匹配；不命中再尝试 last token（"celtics"），
    这样既支持完整名也支持俗称。空 key 直接拒绝，避免 r"\\b\\b" 命中任意文本。
    """

    normalized_team = normalize_team_name(team_key)
    if not normalized_team:
        return False
    if _word_boundary_search(normalized_team, normalized_text):
        return True
    last_token = normalized_team.split()[-1]
    if last_token and last_token != normalized_team:
        return _word_boundary_search(last_token, normalized_text)
    return False


def _word_boundary_search(needle: str, haystack: str) -> bool:
    pattern = r"\b" + re.escape(needle) + r"\b"
    return re.search(pattern, haystack) is not None


__all__ = ["resolve_market_team"]
