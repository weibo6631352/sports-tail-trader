"""量化决策器内部辅助模块。

- helpers: metadata 工具、enrich_decision、bid/tick fallback、tick 对齐
- allocation: 候选过滤、AllocationSnapshot 构造、partial-state 兜底

量化决策器入口在 ``polymarket_trader.workflow.quant_decider``；量化信号入口
（math_prob / goalserve_prob / 三层 max 融合）在
``polymarket_trader.workflow.quant_signal``。
"""

from __future__ import annotations

__all__ = []
