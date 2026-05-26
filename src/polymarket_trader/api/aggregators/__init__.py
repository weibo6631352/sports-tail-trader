"""【api/aggregators】所有 admin/运营/审计查询 endpoint 的统一聚合层。

docs/新架构方案.md §12.2 三类划分 + §12.3 ① 统一聚合：
- **运营查询类**（runtime / portfolio / candidates / markets / sports 等）
  走 runtime 内存 + DataGraph（snapshot-and-release，零 DB），可加 §12.3 ③
  短 TTL 缓存
- **审计查询类**（audit_events / decisions / settlements / analytics 报表 /
  trade timeline / outbox failures）走 DB session_factory，不缓存
- **操盘动作类**走 `app/admin_service.py:AdminService`（仅保留
  `AdminControlsMixin`），不在本层

# 设计约束

- **只读**：aggregator 不写任何 store / 不发 audit / 不调外部 API
- **无状态**：每次调用即时聚合（缓存由 api/middleware/cache 加，仅限
  非审计类 endpoint）
- **field selection**：运营查询默认 summary 字段，`level=detail` 返回完整数据
- **不依赖 AdminService**：所有 aggregator 直接持 `runtime` 和/或
  `session_factory`，与 admin_service.py 完全解耦

# 模块（按职责分）

运营查询（运行时内存 + DataGraph）：
- `runtime_aggregator` —— health / readiness / runtime snapshot / workers /
  metrics / paper-ledger / risk-metrics / outbox queue depth / data freshness
- `portfolio_aggregator` / `position_aggregator` —— 组合暴露 + 持仓
- `candidate_aggregator` —— 策略候选投影（含 3s TTL 缓存）
- `market_misc_aggregator` / `market_detail_aggregator` —— 盘口 / 流动性 /
  数据健康 / 历史快照
- `sports_query_aggregator` / `sports_live_aggregator` —— 直播状态 + 覆盖缺口
- `trading_query_aggregator` —— orders / fills / positions / allocations 列表

审计查询（DB session_factory）：
- `timeline_aggregator` —— audit events / trade replays / trade timeline /
  operator interventions
- `analytics_aggregator` —— edge realization / pnl breakdown / calibration /
  missed opportunities / risk rejections / latency percentiles / equity curve
- `reconcile_decisions_aggregator` —— reconcile diff / decisions dump
- `outbox_aggregator` —— outbox pending / failures
- `settlement_aggregator` —— 市场结算历史

共享 utility：
- `_db` —— `with_repositories` + `RepositoryGroup`
- `_helpers` —— decision_record / latency 分位数纯函数
"""

from .analytics_aggregator import AnalyticsAggregator
from .candidate_aggregator import CandidateAggregator
from .market_detail_aggregator import MarketDetailAggregator
from .market_misc_aggregator import MarketMiscAggregator
from .outbox_aggregator import OutboxAggregator
from .portfolio_aggregator import PortfolioAggregator, PortfolioExposureView
from .position_aggregator import PositionAggregator
from .reconcile_decisions_aggregator import ReconcileDecisionsAggregator
from .runtime_aggregator import RuntimeAggregator
from .settlement_aggregator import SettlementAggregator
from .sports_live_aggregator import SportsLiveAggregator
from .sports_query_aggregator import SportsQueryAggregator
from .timeline_aggregator import TimelineAggregator
from .trading_query_aggregator import TradingQueryAggregator

__all__ = [
    "AnalyticsAggregator",
    "CandidateAggregator",
    "MarketDetailAggregator",
    "MarketMiscAggregator",
    "OutboxAggregator",
    "PortfolioAggregator",
    "PortfolioExposureView",
    "PositionAggregator",
    "ReconcileDecisionsAggregator",
    "RuntimeAggregator",
    "SettlementAggregator",
    "SportsLiveAggregator",
    "SportsQueryAggregator",
    "TimelineAggregator",
    "TradingQueryAggregator",
]
