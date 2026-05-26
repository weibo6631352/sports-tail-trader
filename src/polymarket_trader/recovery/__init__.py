"""【兜底工作流】recovery —— 周期权威校准 + 修复动作执行 + 结算扫描。

docs/新架构方案.md §2 主工作流图右半边。20s 周期 ReconcileWorker 跑权威校准，
发现 drift 时调 ReconcileActionApplier 执行修复（必经 OrderGateway，硬约束
§3）。SettlementScannerService 5min 扫已结算市场触发 redeem。

# 模块

- `reconcile_worker` —— ReconcileWorker / ReconcileWorkerResult / Status
- `reconcile_service` —— ReconcileService / ReconcilePlan / ReconcileAction
- `action_applier` —— ReconcileActionApplier（必经 OrderGateway.review_intent）
- `authority_refresher` —— ReconcileAuthorityRefresher 拉权威源
- `settlement_scanner` —— SettlementScannerService
"""

from __future__ import annotations

from .action_applier import ReconcileActionApplier
from .authority_refresher import (
    AuthoritativeMarketRefresh,
    AuthoritativeRefreshFailure,
    AuthoritativeRefreshSummary,
    DataAuthorityClient,
    GammaMarketCandidate,
    MarketAuthorityClient,
    OrderAuthorityClient,
    ReconcileAuthorityRefresher,
    TradingAuthorityClient,
)
from .reconcile_service import (
    ReconcileAction,
    ReconcileActionType,
    ReconcileMarketPlan,
    ReconcilePlan,
    ReconcileService,
)
from .reconcile_worker import (
    ReconcileWorker,
    ReconcileWorkerResult,
    ReconcileWorkerResultSummary,
    ReconcileWorkerStatus,
)
from .settlement_scanner import SettlementScannerService, SettlementScanResult

__all__ = [
    "AuthoritativeMarketRefresh",
    "AuthoritativeRefreshFailure",
    "AuthoritativeRefreshSummary",
    "DataAuthorityClient",
    "GammaMarketCandidate",
    "MarketAuthorityClient",
    "OrderAuthorityClient",
    "ReconcileAction",
    "ReconcileActionApplier",
    "ReconcileActionType",
    "ReconcileAuthorityRefresher",
    "ReconcileMarketPlan",
    "ReconcilePlan",
    "ReconcileService",
    "ReconcileWorker",
    "ReconcileWorkerResult",
    "ReconcileWorkerResultSummary",
    "ReconcileWorkerStatus",
    "SettlementScanResult",
    "SettlementScannerService",
    "TradingAuthorityClient",
]
