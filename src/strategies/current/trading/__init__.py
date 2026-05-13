"""当前策略的交易决策包。

子模块：
- helpers: metadata 工具、enrich_decision、bid/tick fallback
- hooks: size_entry / decide_entry / decide_exit 三个入场/退出 hook
- follow_up: 成交后续动作（profit-take / auto-exit）
- tail_bypass: 尾盘时间窗口 bypass 决策（何时绕过 endDate 粗筛）
"""

from __future__ import annotations

from .follow_up import decide_follow_up
from .hooks import decide_entry, decide_exit, size_entry

__all__ = ["decide_entry", "decide_exit", "decide_follow_up", "size_entry"]
