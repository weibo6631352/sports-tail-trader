"""【api/aggregators】基于 DataGraph 的只读聚合层。

docs/新架构方案.md §12.3 ①。route 层薄化——所有运营查询类 endpoint
（portfolio / candidates / positions / markets 等）走统一 aggregator，**不
重复跨 store 拼数据**。

# 数据流

```
api/routes/portfolio.py
    ↓
api/aggregators/portfolio_aggregator.py    ← 本目录
    ↓ 读
runtime/data_graph.py (DataGraph)
    ↓ snapshot-and-release
4 store: registry / orderbook_history / account / market_metadata
```

# 设计约束

- **只读**：aggregator 不写任何 store / 不发 audit / 不调外部 API
- **无状态**：每次调用即时聚合，不持有缓存（缓存由 api/middleware/cache 加）
- **基于 DataGraph**：不直接 `runtime.registry.get_*`——必经 DataGraph view
- **field selection**：默认返回 summary 字段，`level=detail` 返回完整数据
  （§12.3 ②）

# 模块

- `portfolio_aggregator` —— 基于 EventView/MarketView 拼 PortfolioExposureView
  示例 aggregator，演示设计模式

后续待迁移（从 app/admin_query/* 搬过来）：
- `candidate_aggregator`
- `market_detail_aggregator`
- `position_aggregator`
"""

from .analytics_aggregator import AnalyticsAggregator
from .candidate_aggregator import CandidateAggregator
from .market_detail_aggregator import MarketDetailAggregator
from .outbox_aggregator import OutboxAggregator
from .portfolio_aggregator import PortfolioAggregator, PortfolioExposureView
from .position_aggregator import PositionAggregator
from .reconcile_decisions_aggregator import ReconcileDecisionsAggregator
from .settlement_aggregator import SettlementAggregator
from .sports_live_aggregator import SportsLiveAggregator
from .timeline_aggregator import TimelineAggregator

__all__ = [
    "AnalyticsAggregator",
    "CandidateAggregator",
    "MarketDetailAggregator",
    "OutboxAggregator",
    "PortfolioAggregator",
    "PortfolioExposureView",
    "PositionAggregator",
    "ReconcileDecisionsAggregator",
    "SettlementAggregator",
    "SportsLiveAggregator",
    "TimelineAggregator",
]
