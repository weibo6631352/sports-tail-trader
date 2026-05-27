import { Navigate, type RouteObject } from 'react-router-dom'
import { AppShell } from './shell/AppShell'
import { StrategyShell, currentStrategyBundle } from '@strategy'
import { lazyNamed } from '@shared/lazyNamed'

// 所有 feature page 走 lazy import：每条路由独立 chunk，首屏只加载 AppShell + 当前路由。
// AppShell.tsx 已在 Outlet 外层挂 Suspense + RouteErrorBoundary，路由切换共享 fallback。

// Live
const LiveOverviewPage = lazyNamed(() => import('@features/live/LiveOverviewPage'), 'LiveOverviewPage')
const CandidatesPage = lazyNamed(() => import('@features/live/CandidatesPage'), 'CandidatesPage')
const LivePositionsPage = lazyNamed(() => import('@features/live/LivePositionsPage'), 'LivePositionsPage')
const LiveOrdersPage = lazyNamed(() => import('@features/live/LiveOrdersPage'), 'LiveOrdersPage')
const OperationsPage = lazyNamed(() => import('@features/live/OperationsPage'), 'OperationsPage')
const GoalserveLivePage = lazyNamed(() => import('@features/live/GoalserveLivePage'), 'GoalserveLivePage')

// Investigate
const TradeTimelinePage = lazyNamed(() => import('@features/investigate/TradeTimelinePage'), 'TradeTimelinePage')
const DecisionsPage = lazyNamed(() => import('@features/investigate/DecisionsPage'), 'DecisionsPage')
const SportsEventsPage = lazyNamed(() => import('@features/investigate/SportsEventsPage'), 'SportsEventsPage')
const AuditPage = lazyNamed(() => import('@features/investigate/AuditPage'), 'AuditPage')

// Analytics
const FunnelPage = lazyNamed(() => import('@features/analytics/FunnelPage'), 'FunnelPage')
const RejectionsPage = lazyNamed(() => import('@features/analytics/RejectionsPage'), 'RejectionsPage')
const RiskRejectionsPage = lazyNamed(() => import('@features/analytics/RiskRejectionsPage'), 'RiskRejectionsPage')
const EdgeRealizationPage = lazyNamed(() => import('@features/analytics/EdgeRealizationPage'), 'EdgeRealizationPage')
const CalibrationPage = lazyNamed(() => import('@features/analytics/CalibrationPage'), 'CalibrationPage')
const ExecutionQualityPage = lazyNamed(() => import('@features/analytics/ExecutionQualityPage'), 'ExecutionQualityPage')
const PnlBreakdownPage = lazyNamed(() => import('@features/analytics/PnlBreakdownPage'), 'PnlBreakdownPage')
const MissedOpportunitiesPage = lazyNamed(() => import('@features/analytics/MissedOpportunitiesPage'), 'MissedOpportunitiesPage')
const LatencyPage = lazyNamed(() => import('@features/analytics/LatencyPage'), 'LatencyPage')

// Markets
const MarketsListPage = lazyNamed(() => import('@features/markets/MarketsListPage'), 'MarketsListPage')
const SettlementsPage = lazyNamed(() => import('@features/markets/SettlementsPage'), 'SettlementsPage')

// Ops
const PortfolioPage = lazyNamed(() => import('@features/ops/PortfolioPage'), 'PortfolioPage')
const OrdersPage = lazyNamed(() => import('@features/ops/OrdersPage'), 'OrdersPage')
const PositionsPage = lazyNamed(() => import('@features/ops/PositionsPage'), 'PositionsPage')
const FillsPage = lazyNamed(() => import('@features/ops/FillsPage'), 'FillsPage')
const AllocationsPage = lazyNamed(() => import('@features/ops/AllocationsPage'), 'AllocationsPage')
const AllocationDecisionsPage = lazyNamed(
  () => import('@features/ops/AllocationDecisionsPage'),
  'AllocationDecisionsPage',
)
const ExportsPage = lazyNamed(() => import('@features/ops/ExportsPage'), 'ExportsPage')

// System
const HealthPage = lazyNamed(() => import('@features/system/HealthPage'), 'HealthPage')
const OutboxPage = lazyNamed(() => import('@features/system/OutboxPage'), 'OutboxPage')
const ReconcilePage = lazyNamed(() => import('@features/system/ReconcilePage'), 'ReconcilePage')

export const routes: RouteObject[] = [
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to="/live" replace /> },

      // 盯盘
      { path: 'live', element: <LiveOverviewPage /> },
      { path: 'live/candidates', element: <CandidatesPage /> },
      { path: 'live/positions', element: <LivePositionsPage /> },
      { path: 'live/orders', element: <LiveOrdersPage /> },
      { path: 'live/operations', element: <OperationsPage /> },
      { path: 'live/goalserve', element: <GoalserveLivePage /> },

      // 复盘
      { path: 'investigate/timeline', element: <TradeTimelinePage /> },
      { path: 'investigate/decisions', element: <DecisionsPage /> },
      { path: 'investigate/sports-events', element: <SportsEventsPage /> },
      { path: 'investigate/audit', element: <AuditPage /> },

      // 分析
      { path: 'analytics/funnel', element: <FunnelPage /> },
      { path: 'analytics/rejections', element: <RejectionsPage /> },
      { path: 'analytics/risk-rejections', element: <RiskRejectionsPage /> },
      { path: 'analytics/edge-realization', element: <EdgeRealizationPage /> },
      { path: 'analytics/calibration', element: <CalibrationPage /> },
      { path: 'analytics/execution-quality', element: <ExecutionQualityPage /> },
      { path: 'analytics/pnl-breakdown', element: <PnlBreakdownPage /> },
      { path: 'analytics/missed-opportunities', element: <MissedOpportunitiesPage /> },
      { path: 'analytics/latency', element: <LatencyPage /> },

      // 市场
      { path: 'markets', element: <MarketsListPage /> },
      { path: 'markets/settlements', element: <SettlementsPage /> },

      // Portfolio / 订单 / 持仓 / 流水 / 分配 / 导出
      { path: 'portfolio', element: <PortfolioPage /> },
      { path: 'orders', element: <OrdersPage /> },
      { path: 'positions', element: <PositionsPage /> },
      { path: 'fills', element: <FillsPage /> },
      { path: 'allocations', element: <AllocationsPage /> },
      { path: 'allocations/decisions', element: <AllocationDecisionsPage /> },
      { path: 'exports', element: <ExportsPage /> },

      // 系统
      { path: 'system/health', element: <HealthPage /> },
      { path: 'system/outbox', element: <OutboxPage /> },
      { path: 'system/reconcile', element: <ReconcilePage /> },

      // 策略：bundle 注入子路由（bundle 内部组件已 lazy）
      {
        path: 'strategy',
        element: <StrategyShell />,
        children: currentStrategyBundle.routes ?? [],
      },

      { path: '*', element: <Navigate to="/live" replace /> },
    ],
  },
]
