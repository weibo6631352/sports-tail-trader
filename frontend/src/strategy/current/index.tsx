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
const StrategyConfigPage = lazyNamed(() => import('./StrategyConfigPage'), 'StrategyConfigPage')
const StrategyDiagnosticsPage = lazyNamed(
  () => import('./StrategyDiagnosticsPage'),
  'StrategyDiagnosticsPage',
)
const ParameterSweepPage = lazyNamed(
  () => import('./ParameterSweepPage'),
  'ParameterSweepPage',
)

export const currentStrategyBundle: StrategyBundle = {
  id: 'sports_tail',
  displayName: '体育扫尾量化策略 (sports_tail)',
  routes: [
    { index: true, element: <StrategyOverviewPage /> },
    { path: 'config', element: <StrategyConfigPage /> },
    { path: 'diagnostics', element: <StrategyDiagnosticsPage /> },
    { path: 'parameter-sweep', element: <ParameterSweepPage /> },
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
