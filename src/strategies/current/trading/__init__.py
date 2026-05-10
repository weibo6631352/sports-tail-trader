"""当前策略的交易决策包。

从原 ``trading.py`` 拆分而来：
- helpers: metadata 读取
- pricing: 价格上限与锁定信号
- matching: 主客队映射
- exit_overlay: profit-take + 资金占用效率门禁
- risk_limits: plan 级风控修正与成交统计
- gates: 入场/分配门禁
- allocation: 候选快照与 skip 原因
- hooks: ``size_entry`` / ``decide_entry`` / ``decide_exit`` 三个公开 hook

外部按 ``from strategies.current.trading import size_entry`` 等保持不变。
"""

from __future__ import annotations

from .hooks import decide_entry, decide_exit, size_entry

__all__ = ["decide_entry", "decide_exit", "size_entry"]
