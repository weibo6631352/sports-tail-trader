import type { StrategyBundle } from '../api'
import { CandidatesWidget } from './widgets/CandidatesWidget'
import { marketRowBadges } from './widgets/marketBadges'
import { MarketDetailPanel } from './widgets/MarketDetailPanel'
import { lazyNamed } from '@shared/lazyNamed'

// 策略私有页面走 lazy（路由切到才下载）；
// dashboardWidgets / marketRowBadges / marketDetailPanel 是即时渲染钩子——
// 直接 import 进 bundle，体积可控（< 5KB gzip）。
// 参数元数据仅做 UI overlay；schema 真相在后端 /parameters registry。详见 ./paramMetadata.ts。

const StrategyOverviewPage = lazyNamed(
  () => import('./StrategyOverviewPage'),
  'StrategyOverviewPage',
)
const StrategyDiagnosticsPage = lazyNamed(
  () => import('./StrategyDiagnosticsPage'),
  'StrategyDiagnosticsPage',
)
// StrategyConfigPage / ParameterSweepPage 依赖 /parameters + /operations/parameter-sweep
// 后端端点不存在(已删),前端是僵尸页面 → 已移除.

export const currentStrategyBundle: StrategyBundle = {
  id: 'sports_tail',
  displayName: '量化量化策略 (sports_tail)',
  routes: [
    { index: true, element: <StrategyOverviewPage /> },
    { path: 'diagnostics', element: <StrategyDiagnosticsPage /> },
  ],
  dashboardWidgets: [
    {
      id: 'current.candidates',
      title: '可确认候选',
      render: () => <CandidatesWidget />,
    },
  ],
  marketRowBadges,
  marketDetailPanel: (market) => <MarketDetailPanel market={market} />,
}
