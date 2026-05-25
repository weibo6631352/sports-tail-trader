"""下单幂等 key 构造工具。

策略不应自己拼字符串，避免不同策略 / 不同决策路径生成重复或冲突的 key。
"""

from __future__ import annotations

import hashlib

from polymarket_trader.contracts.decisions import DecisionKind


def build_entry_key(
    *,
    condition_id: str,
    token_id: str,
    decision_kind: DecisionKind,
    window: str,
) -> str:
    """构造入场幂等 key。

    ``window`` 由策略选定（如 "2026-05-10T12:30Z"、"q4_tail"），表达"在该窗口内
    最多下一次"的语义；同 window 内的重复决策共享同一个 key，避免重复下单。
    """

    base = f"{condition_id}:{token_id}:{decision_kind.value}:{window}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]
    return f"entry-{decision_kind.value}-{digest}"
