"""TOTAL_GAMES outcome 解析：从 outcome label / market question 提取 (line, direction)。

典型 Polymarket outcome label：
- ``"Over 5.5"``、``"Under 6"``、``"Over 4.5 games"``
- 二元 YES/NO：``"Yes"`` / ``"No"``，方向与 line 写在 market_question / market_name 里
  （例如 ``"Will the series go over 5.5 games?"``）

返回 ``(line, direction)``，direction ∈ {"over", "under"}。
- 类别型 outcome 优先看 outcome_label 自身。
- YES 默认锚定 question 表述方向；NO 取反。
- 失败一律返回 ``None``，evaluator 据此报 ``SERIES_OUTCOME_NOT_PARSED``。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Literal


Direction = Literal["over", "under"]

_OVER_UNDER_PATTERN = re.compile(
    r"\b(over|under)\s+(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_BINARY_OUTCOMES = frozenset({"yes", "no"})


def parse_total_games_outcome(
    outcome_label: str,
    market_question: str | None,
) -> tuple[Decimal, Direction] | None:
    """从 outcome_label + market_question 解析 (line, direction)。

    优先级：
    1. ``outcome_label`` 直接命中 ``over/under <num>`` → 返回该 line/direction。
    2. outcome 是 yes/no：在 market_question 里找 over/under：
       - YES → 保持原方向
       - NO  → 翻转方向（over → under，反之）
    3. 否则 None。
    """

    label = (outcome_label or "").strip()
    question = (market_question or "").strip()

    primary = _scan(label)
    if primary is not None:
        return primary

    label_norm = label.lower().strip(" .?!")
    if label_norm in _BINARY_OUTCOMES:
        question_hit = _scan(question)
        if question_hit is None:
            return None
        line, direction = question_hit
        if label_norm == "no":
            direction = "under" if direction == "over" else "over"
        return line, direction
    return None


def _scan(text: str) -> tuple[Decimal, Direction] | None:
    if not text:
        return None
    match = _OVER_UNDER_PATTERN.search(text)
    if match is None:
        return None
    direction_raw = match.group(1).lower()
    line_raw = match.group(2)
    try:
        line = Decimal(line_raw)
    except InvalidOperation:
        return None
    direction: Direction = "over" if direction_raw == "over" else "under"
    return line, direction


__all__ = ["Direction", "parse_total_games_outcome"]
