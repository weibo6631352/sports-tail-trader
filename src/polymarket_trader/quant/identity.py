"""当前策略身份常量。

``STRATEGY_ID`` 是策略实例在 DB schema / 查询过滤层的唯一标识；
框架按该值作为 orders / fills / positions / allocations / audit_events /
decision_records 等 SCOPE 表的归属键。

一旦确定，禁止运行时变更；如需迁移到新策略实例，必须新建包并使用新 id。
"""

from __future__ import annotations

# 体育扫尾策略当前实例 id。命名与策略业务定位一致 (sports tail trader)。
STRATEGY_ID: str = "sports_tail"

__all__ = ["STRATEGY_ID"]
