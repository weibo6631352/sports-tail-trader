"""体育队名归一化：跨 tail/outright 共用一份语义。

规则单一来源：``tail/mlb._normalize_team_name`` 历史上自带轻量归一（lower + `&`→`and` +
空白压缩），但 outright 需要从市场问题文本中反向匹配球队，标点（`.` `,` `'`）会
干扰 word-boundary 匹配，所以这里把规则扩展为去掉常见标点再压缩空白。

只放在 ``current/_shared/``，不进 domain：当前策略包专有，不上升为框架级 API。
"""

from __future__ import annotations

# 替换为空白后再压缩；句点单独处理（直接吞掉，让 "L.A." 与 "LA" 等价）。
_PUNCT_TO_SPACE = ",;:!?\"()[]{}<>/\\"
_PUNCT_TO_EMPTY = ".'"


def normalize_team_name(value: str | None) -> str:
    """Lowercase, `&` → `and`，剔除标点，压缩空白。

    设计意图：
    - lowercase 让 "Celtics" / "celtics" 等价。
    - `&` → `and` 让 "Texas A&M" / "Texas A and M" 等价。
    - 撇号单引号要在 split 之前去掉（"L'Equipe" → "lequipe"），否则
      "L.A. Lakers" 与 "LA Lakers" 不能命中同一 word boundary。
    - 其他标点（句点、逗号、问号等）整体清掉，避免 `re.search(r"\\b...")`
      在 "Will Celtics win 2026 NBA championship?" 这种文本上漏匹配。
    """

    raw = str(value or "")
    if not raw:
        return ""
    lowered = raw.lower().replace("&", " and ")
    cleaned_chars = []
    for ch in lowered:
        if ch in _PUNCT_TO_EMPTY:
            # 句点 / 撇号直接吞掉：保证 "L.A. Lakers" 与 "LA Lakers" 等价，
            # "L'Equipe" 与 "LEquipe" 等价；否则后续 word-boundary 会把 "L A"
            # 拆开导致与 "LA" 失配。
            continue
        if ch in _PUNCT_TO_SPACE:
            cleaned_chars.append(" ")
        else:
            cleaned_chars.append(ch)
    return " ".join("".join(cleaned_chars).split())


__all__ = ["normalize_team_name"]
