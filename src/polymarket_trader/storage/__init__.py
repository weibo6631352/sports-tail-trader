"""框架级内存 store。

与 ``runtime`` 的运行时只写不读访问点不同，``storage`` 暴露给跨 worker、
跨决策路径只读消费的数据快照（典型代表：赛季积分、隐含概率）。
"""

from polymarket_trader.storage.season_state_store import SeasonStateStore

__all__ = ["SeasonStateStore"]
