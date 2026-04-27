import { Suspense, lazy } from 'react'
import { Route, Routes } from 'react-router-dom'
import { AppShell } from '../shared/layout/AppShell'

const DashboardPage = lazy(() =>
  import('../features/dashboard/DashboardPage').then((module) => ({ default: module.DashboardPage })),
)
const MarketsPage = lazy(() =>
  import('../features/markets/MarketsPage').then((module) => ({ default: module.MarketsPage })),
)
const CandidatesPage = lazy(() =>
  import('../features/candidates/CandidatesPage').then((module) => ({ default: module.CandidatesPage })),
)
const OrdersPage = lazy(() =>
  import('../features/orders/OrdersPage').then((module) => ({ default: module.OrdersPage })),
)
const PositionsPage = lazy(() =>
  import('../features/positions/PositionsPage').then((module) => ({ default: module.PositionsPage })),
)
const TradeReplaysPage = lazy(() =>
  import('../features/replay/TradeReplaysPage').then((module) => ({ default: module.TradeReplaysPage })),
)
const AuditPage = lazy(() =>
  import('../features/audit/AuditPage').then((module) => ({ default: module.AuditPage })),
)
const OperationsPage = lazy(() =>
  import('../features/operations/OperationsPage').then((module) => ({ default: module.OperationsPage })),
)

export const App = () => {
  return (
    <AppShell>
      <Suspense fallback={<div className="page-loading">页面加载中...</div>}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/markets" element={<MarketsPage />} />
          <Route path="/candidates" element={<CandidatesPage />} />
          <Route path="/orders" element={<OrdersPage />} />
          <Route path="/positions" element={<PositionsPage />} />
          <Route path="/replay" element={<TradeReplaysPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/operations" element={<OperationsPage />} />
        </Routes>
      </Suspense>
    </AppShell>
  )
}
