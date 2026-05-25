"""HANDICAP outcome 解析：从 outcome label / market question 提取 (team_side, handicap, scope)。

典型 Polymarket outcome：
- ``"Celtics -1.5"`` / ``"Knicks +1.5"`` —— 类别型 outcome 自带 line。
- 二元 YES/NO，line 在 market_question 里：
  ``"Will the Celtics cover -1.5 games in the series?"``、
  ``"Will Boston cover +3.5 in Game 5?"``

scope 判定：
- 关键词包含 ``"game N"``、``"game spread"``、``"game handicap"`` → single_game。
- 否则（含 ``"series handicap"`` 或无明显限定）→ series。

返回 ``HandicapBet(team_side, handicap, scope)``；team_side ∈ {"team_a", "team_b"}
表示 outcome 押注的是 series_state 的 team_a 还是 team_b。匹配失败返回 None。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from polymarket_trader.domain.market import Market
from polymarket_trader.quant._shared.team_normalize import normalize_team_name
from polymarket_trader.quant.series.types import SeriesState


HandicapScope = Literal["single_game", "series"]
TeamSide = Literal["team_a", "team_b"]


@dataclass(frozen=True, slots=True)
class HandicapBet:
    """单条 handicap 注的解析结果。

    ``handicap`` 字段始终以 ``team_side`` 视角表达：负数表示该队让分，正数表示
    该队受让；定价模型用同一签名约定。
    """

    team_side: TeamSide
    handicap: Decimal
    scope: HandicapScope


_TEAM_LINE_PATTERN = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 .'&-]*?)\s*([+-]\d+(?:\.\d+)?)\s*$",
)
_LINE_PATTERN = re.compile(r"([+-]\d+(?:\.\d+)?)")
_BINARY_OUTCOMES = frozenset({"yes", "no"})
_SINGLE_GAME_HINTS = ("game spread", "game handicap")
# "game N" 形态（game 1, game 7, ...）：单独写正则避免误吞 "series game"。
_GAME_N_PATTERN = re.compile(r"\bgame\s+\d+\b", re.IGNORECASE)


def parse_handicap_outcome(
    outcome_label: str,
    market: Market,
    state: SeriesState,
) -> HandicapBet | None:
    """从 outcome_label + market_question 解析 HandicapBet。

    步骤：
    1. 类别型 outcome 直接命中 ``Team [+-]X``：team_side 由 normalize 后球队名比对得到。
    2. YES/NO outcome：球队从 question 里反向匹配，line 从 question 里提取，
       NO 翻转符号（"Yes Celtics -1.5" ⇔ "No Celtics +1.5"）。
    3. scope 由 question + outcome 联合关键词决定（single_game vs series）。
    """

    label = (outcome_label or "").strip()
    question = (market.market_question or "")
    market_name = (market.market_name or "")
    combined_text = f"{label} {question} {market_name}".strip()
    scope = _classify_scope(combined_text)

    # 1) 类别型 outcome：直接抓 team + line。
    team_match = _TEAM_LINE_PATTERN.match(label)
    if team_match:
        team_raw, line_raw = team_match.group(1), team_match.group(2)
        side = _team_side(team_raw, state)
        line = _decimal(line_raw)
        if side is not None and line is not None:
            return HandicapBet(team_side=side, handicap=line, scope=scope)

    # 2) YES/NO：从 question 找球队 + line。
    label_norm = label.lower().strip(" .?!")
    if label_norm in _BINARY_OUTCOMES:
        side = _team_side_from_text(question, state) or _team_side_from_text(market_name, state)
        line_match = _LINE_PATTERN.search(question) or _LINE_PATTERN.search(market_name)
        if side is None or line_match is None:
            return None
        line = _decimal(line_match.group(1))
        if line is None:
            return None
        if label_norm == "no":
            line = -line
            # team_side 不变：NO 仍是押同一支球队，只是反向 line。
        return HandicapBet(team_side=side, handicap=line, scope=scope)

    return None


def _classify_scope(text: str) -> HandicapScope:
    lower = text.lower()
    if any(hint in lower for hint in _SINGLE_GAME_HINTS):
        return "single_game"
    if _GAME_N_PATTERN.search(lower):
        return "single_game"
    return "series"


def _team_side(raw_team: str, state: SeriesState) -> TeamSide | None:
    raw_norm = normalize_team_name(raw_team)
    a_norm = normalize_team_name(state.team_a)
    b_norm = normalize_team_name(state.team_b)
    if not raw_norm or not a_norm or not b_norm:
        return None
    if raw_norm == a_norm or raw_norm.split()[-1] == a_norm.split()[-1]:
        # last-token 兜底处理 "Celtics" 对 "Boston Celtics"。
        return "team_a"
    if raw_norm == b_norm or raw_norm.split()[-1] == b_norm.split()[-1]:
        return "team_b"
    return None


def _team_side_from_text(text: str, state: SeriesState) -> TeamSide | None:
    text_norm = normalize_team_name(text)
    a_norm = normalize_team_name(state.team_a)
    b_norm = normalize_team_name(state.team_b)
    if not text_norm or not a_norm or not b_norm:
        return None
    a_hit = _word_hit(text_norm, a_norm)
    b_hit = _word_hit(text_norm, b_norm)
    if a_hit and not b_hit:
        return "team_a"
    if b_hit and not a_hit:
        return "team_b"
    return None


def _word_hit(text_norm: str, team_norm: str) -> bool:
    if not team_norm:
        return False
    if re.search(r"\b" + re.escape(team_norm) + r"\b", text_norm):
        return True
    last = team_norm.split()[-1]
    if last and last != team_norm:
        return re.search(r"\b" + re.escape(last) + r"\b", text_norm) is not None
    return False


def _decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


__all__ = ["HandicapBet", "HandicapScope", "TeamSide", "parse_handicap_outcome"]
