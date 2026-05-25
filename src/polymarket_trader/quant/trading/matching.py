"""把直播源的主客队映射到 Polymarket outcome token 上。

Polymarket outcomes 的展示顺序不稳定，常见“客队 vs 主队”的反序；策略评估
要求 ``HOME/AWAY`` 与直播源一致，因此入场前需要按 outcome 文案做一次别名匹配
重映射。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Mapping

from polymarket_trader.quant.outcomes import SportsTokenTarget, target_for_token
from polymarket_trader.quant.tail import LiveGameState, SportsMarketSide


_GENERIC_COMPETITOR_TOKENS = {
    "a",
    "an",
    "and",
    "at",
    "club",
    "fc",
    "game",
    "match",
    "men",
    "team",
    "the",
    "to",
    "vs",
    "v",
    "women",
}


def _target_for_live_game(
    market,
    token_id: str | None,
    *,
    metadata: Mapping[str, Any],
    game: LiveGameState | None,
) -> tuple[SportsTokenTarget | None, str]:
    """用直播源主客队修正 side token 的 HOME/AWAY 方向。

    Polymarket 体育 market 的 outcomes 顺序不稳定，尤其常见“客队 vs 主队”的
    展示顺序。只在有直播比赛状态时按队名重映射方向；没有直播状态时保留
    descriptor 的静态结果，让历史回放和非体育兜底路径不被误伤。
    """

    target = target_for_token(market, token_id)
    if target is None:
        return None, "unsupported_token"
    if target.side not in {SportsMarketSide.HOME, SportsMarketSide.AWAY}:
        return target, ""
    if game is None:
        return target, ""

    # 单场 Yes/No 胜负盘：两个 token 的 label 都是问句点名的同一支球队，先按它
    # 解出该球队对应直播源的 HOME/AWAY；"No" token（invert_side=True）结算的是
    # 对手获胜，需把方向翻转。team-name-outcome 胜负盘 invert_side 恒为 False，
    # 行为与改动前完全一致。
    live_side = _live_side_for_outcome_label(target.label, game=game, metadata=metadata)
    if live_side is None:
        return None, "token_live_side_mismatch"
    if target.invert_side:
        live_side = (
            SportsMarketSide.AWAY if live_side == SportsMarketSide.HOME else SportsMarketSide.HOME
        )
    return replace(target, side=live_side), ""


def _live_side_for_outcome_label(
    label: str,
    *,
    game: LiveGameState,
    metadata: Mapping[str, Any],
) -> SportsMarketSide | None:
    """根据 token 文案判断它对应直播源的主队还是客队。"""

    label_text = _normalize_competitor_text(label)
    if not label_text:
        return None

    match_metadata = metadata.get("live_match")
    match_mapping = match_metadata if isinstance(match_metadata, Mapping) else {}
    home_aliases = _competitor_aliases(
        game.home_name,
        match_mapping.get("matched_home_alias"),
        match_mapping.get("home_name"),
    )
    away_aliases = _competitor_aliases(
        game.away_name,
        match_mapping.get("matched_away_alias"),
        match_mapping.get("away_name"),
    )
    home_score = _label_alias_score(label_text, home_aliases)
    away_score = _label_alias_score(label_text, away_aliases)
    if home_score > away_score:
        return SportsMarketSide.HOME
    if away_score > home_score:
        return SportsMarketSide.AWAY
    return None


def _competitor_aliases(*values: object) -> set[str]:
    """生成队名匹配别名，兼容全名、队名末尾和较长单词 token。"""

    aliases: set[str] = set()
    for value in values:
        normalized = _normalize_competitor_text(value)
        if not normalized:
            continue
        tokens = tuple(token for token in normalized.split() if token not in _GENERIC_COMPETITOR_TOKENS)
        if not tokens:
            continue
        phrase = " ".join(tokens)
        aliases.add(phrase)
        if len(tokens) >= 2:
            aliases.add(tokens[-1])
        aliases.update(token for token in tokens if len(token) >= 3)
    return aliases


def _label_alias_score(label_text: str, aliases: set[str]) -> int:
    """返回 outcome 文案命中一侧别名的强度，完整队名会强于共享地区词。"""

    padded = f" {label_text} "
    return max(
        (
            len(alias.replace(" ", ""))
            for alias in aliases
            if alias and (label_text == alias or f" {alias} " in padded)
        ),
        default=0,
    )


def _normalize_competitor_text(value: object) -> str:
    text = str(value or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())
