"""【层 4：反馈】pipeline/feedback/ —— 把链上撮合结果回灌 AccountStateStore。

原架构方案 §2 主工作流图层 4。link 下两类 writer 通过 §19.3 单 writer
原则互斥：

# 单 writer 矩阵

| 模式 | balance / positions / fills 唯一 writer | 实现 |
|---|---|---|
| live (paper_trading_mode=false) | `user_ws_worker` projection | Polymarket user-ws push 收到 BALANCE / POSITION / TRADE 事件 → 写 store |
| paper (paper_trading_mode=true) | `paper_balance_syncer` | `runtime/paper_background_tasks.py` 每秒从 `paper_ledger` 投影到 store |

两种 writer **互斥**——paper 模式下 user-ws 不连真 Polymarket，paper_balance_syncer
独占；live 模式下不启 paper_balance_syncer，user_ws projection 独占。`reconcile`
worker 已通过 `refresh_account_inline=False` 关闭用户态拉取，不抢同一份 store。

# 备用机制：UserAccountPoller（live 模式可选）

`user_account_poller` 是周期 polling 兜底——当 user-ws 漏推 / 长断线时，从
`data_client.list_positions` + `clob_client.list_open_orders` + `get_balance_allowance`
拉权威快照写 store。当前 main.py 未启用（user-ws 推送可靠），保留代码以备
未来需要时一键 wire。启用时它替代 user_ws projection 成为 live writer——
**两者不能同时启**（违反 §19.3 单 writer）。

# 模块

- `user_ws/` —— `UserWsWorker` + `UserAccountStateProjection`（项目）
- `user_account_poller` —— `UserAccountPoller`（备用）

paper 模式 syncer 留在 `runtime/paper_background_tasks.py`（与其它 paper
background tasks 一起编排，访问 paper_ledger 直接），符合"writer = 离数据
源近"原则。
"""

from .user_account_poller import UserAccountPoller
from .user_ws import UserWsWorker

__all__ = ["UserAccountPoller", "UserWsWorker"]
