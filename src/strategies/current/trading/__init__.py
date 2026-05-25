"""当前策略的交易决策包。

子模块：
- helpers: metadata 工具、enrich_decision、bid/tick fallback
- hooks: size_entry / decide_entry 入场 hook（暂时保留，未来由 QuantDecider 接管）

量化决策器：``strategies.current.quant_decider.QuantDecider``（类，独立模块）。
"""

from __future__ import annotations

from .hooks import decide_entry, size_entry

__all__ = ["decide_entry", "size_entry"]
