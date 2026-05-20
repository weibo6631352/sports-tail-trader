// 顶层导航分组。每条 Link 对应 routes.tsx 里的一条路由。
// P1/P2 端点没就绪的路由仍然挂出来，组件内部 EmptyState 占位——保持 IA 稳定。

export type NavLink = {
  path: string
  label: string
  badge?: 'P0' | 'P1' | 'P2' | string
}

export type NavGroup = {
  id: string
  label: string
  links: NavLink[]
}

export const NAV_GROUPS: NavGroup[] = [
  {
    id: 'live',
    label: '盯盘 Live',
    links: [
      { path: '/live', label: '总览' },
      { path: '/live/goalserve', label: 'Goalserve 直播', badge: 'P0' },
      { path: '/live/candidates', label: '候选' },
      { path: '/live/positions', label: '实时持仓' },
      { path: '/live/orders', label: '实时挂单' },
      { path: '/live/operations', label: '手工干预' },
    ],
  },
  {
    id: 'strategy',
    label: '策略 Strategy',
    links: [
      { path: '/strategy', label: '策略概览' },
      { path: '/strategy/config', label: '参数热调', badge: 'P0' },
      { path: '/strategy/diagnostics', label: '策略诊断' },
      { path: '/strategy/parameter-sweep', label: '参数扫描', badge: 'P2' },
    ],
  },
  {
    id: 'investigate',
    label: '调查 Investigate',
    links: [
      { path: '/investigate/timeline', label: '交易时间线', badge: 'P0' },
      { path: '/investigate/decisions', label: '决策记录', badge: 'P0' },
      { path: '/investigate/sports-events', label: '体育事件', badge: 'P0' },
      { path: '/investigate/audit', label: '审计' },
      { path: '/investigate/orderbook-replay', label: '盘口回放', badge: 'P1' },
    ],
  },
  {
    id: 'analytics',
    label: '分析 Analytics',
    links: [
      { path: '/analytics/funnel', label: '决策漏斗' },
      { path: '/analytics/execution-quality', label: '执行质量' },
      { path: '/analytics/edge-realization', label: '边际实现', badge: 'P0' },
      { path: '/analytics/pnl-breakdown', label: 'PnL 分解', badge: 'P1' },
      { path: '/analytics/calibration', label: '胜率校准', badge: 'P0' },
      { path: '/analytics/rejections', label: '拒绝原因' },
      { path: '/analytics/risk-rejections', label: '风控拒绝', badge: 'P0' },
      { path: '/analytics/missed-opportunities', label: '拒绝盈亏', badge: 'P1' },
      { path: '/analytics/latency', label: '延迟分位', badge: 'P0' },
    ],
  },
  {
    id: 'markets',
    label: '市场 Markets',
    links: [
      { path: '/markets', label: '市场列表' },
      { path: '/markets/settlements', label: '结算对照', badge: 'P1' },
    ],
  },
  {
    id: 'ops',
    label: '运营 Ops',
    links: [
      { path: '/portfolio', label: 'Portfolio 概览', badge: 'P2' },
      { path: '/orders', label: '历史订单' },
      { path: '/fills', label: '成交流水' },
      { path: '/allocations', label: '资金分配' },
      { path: '/allocations/decisions', label: '分配决策', badge: 'P1' },
      { path: '/exports', label: '导出中心' },
    ],
  },
  {
    id: 'system',
    label: '系统 System',
    links: [
      { path: '/system/health', label: '健康状态' },
      { path: '/system/outbox', label: 'Outbox 队列' },
      { path: '/system/reconcile', label: 'Reconcile' },
    ],
  },
]
