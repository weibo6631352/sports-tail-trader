"""当前策略的交易决策子模块。

子模块（全部供 QuantDecider 类内部使用，不直接暴露给 framework）：
- helpers: metadata 工具、enrich_decision、bid/tick fallback
- allocation: 候选过滤、Kelly 分配辅助
- exit_overlay: 动态退出 / profit_take metadata 工具
- gates: ask depth / open order 工具
- matching: 直播状态匹配
- pricing: 价格上限

量化决策器入口在 ``polymarket_trader.workflow.quant_decider.QuantDecider``。
"""

from __future__ import annotations

__all__ = []
