"""把 Polymarket series winner 子市场 outcome 映射到 SeriesState 的 team_a / team_b。

规则：
1. outcome label 直接是球队名：normalize 后与 state.team_a / team_b 比对。
   先精确匹配；不中则走昵称扩展（如 "Cavs" → "cavaliers"）再重试。
2. YES / NO 二元 outcome：从 market_question / event_title 反向匹配 team_a 或
   team_b 的全名或 last token。YES → 该队赢；NO → 对手赢。
3. 命中 0 或多于 1 视为歧义，返回 None；evaluator 据此报 SERIES_TEAM_NOT_RESOLVED。

复用 ``_shared/team_normalize.normalize_team_name``——避免 series / outright 各
自一份归一化。
"""

from __future__ import annotations

import re
from typing import Literal

from polymarket_trader.domain.market import Market
from polymarket_trader.workflow._shared.team_normalize import normalize_team_name
from polymarket_trader.workflow.series.types import SeriesState


TeamSide = Literal["team_a", "team_b"]

_BINARY_OUTCOMES = frozenset({"yes", "no"})

# Polymarket outcome 常见昵称 → 官方队名中会出现的规范词。
# key: normalize_team_name(nickname), value: normalize_team_name(canonical_token)
# 精确匹配失败时用这张表做二次扩展，避免逐队硬编码同时保持可审计。
_NICKNAME_EXPANSIONS: dict[str, str] = {
    # NBA
    "cavs": "cavaliers",
    "wolves": "timberwolves",
    "blazers": "trail blazers",
    "okc": "thunder",
    # NHL
    "habs": "canadiens",
    "avs": "avalanche",
    "preds": "predators",
    "bolts": "lightning",
    "sens": "senators",
    "leafs": "maple leafs",
}


def resolve_series_team(
    market: Market,
    outcome_label: str,
    state: SeriesState,
) -> TeamSide | None:
    """从市场文本反向解析 series winner outcome 指代的队伍。

    返回 ``"team_a"`` / ``"team_b"`` / ``None``。
    """

    label_norm = normalize_team_name(outcome_label)
    team_a_norm = normalize_team_name(state.team_a)
    team_b_norm = normalize_team_name(state.team_b)
    if not team_a_norm or not team_b_norm:
        return None

    if label_norm and label_norm not in _BINARY_OUTCOMES:
        # 类别型 outcome：先精确匹配，再走昵称扩展。
        match = _match_side(label_norm, team_a_norm, team_b_norm)
        if match is None:
            expanded = _NICKNAME_EXPANSIONS.get(label_norm)
            if expanded:
                match = _match_side(expanded, team_a_norm, team_b_norm)
        return match

    # YES / NO outcome：从 market_question 反向找球队。系列赛市场 YES/NO 通常表达
    # 形如 "Will Celtics win the series?"，单边出现；event_title / event_slug 常
    # 同时含两队（"Celtics vs Knicks"），引入歧义，所以优先只看 question。
    primary_text = normalize_team_name(market.market_question or "")
    side = _match_side(primary_text, team_a_norm, team_b_norm) if primary_text else None
    if side is None:
        # 二级 fallback：用 market_name（同样单边语义概率更大）。
        secondary_text = normalize_team_name(market.market_name or "")
        side = _match_side(secondary_text, team_a_norm, team_b_norm) if secondary_text else None
    if side is None:
        return None
    if label_norm == "yes":
        return side
    if label_norm == "no":
        return "team_b" if side == "team_a" else "team_a"
    return None


def _match_side(text: str, team_a: str, team_b: str) -> TeamSide | None:
    """``text`` 中只有一支球队 word boundary 命中时返回那一边；歧义/缺失返回 None。"""

    a_hits = _team_in_text(team_a, text)
    b_hits = _team_in_text(team_b, text)
    if a_hits and not b_hits:
        return "team_a"
    if b_hits and not a_hits:
        return "team_b"
    return None


def _team_in_text(team_norm: str, text_norm: str) -> bool:
    if not team_norm:
        return False
    if _word_boundary(team_norm, text_norm):
        return True
    last = team_norm.split()[-1]
    if last and last != team_norm:
        if _word_boundary(last, text_norm):
            return True
        # Compound-word fallback: "timberwolves" ends with "wolves"
        for word in text_norm.split():
            if word.endswith(last):
                return True
    return False


def _word_boundary(needle: str, haystack: str) -> bool:
    pattern = r"\b" + re.escape(needle) + r"\b"
    return re.search(pattern, haystack) is not None


__all__ = ["TeamSide", "resolve_series_team"]
