// 中心化 React Query keys。SSE 事件 → query key invalidation 在 core/sse 里
// 引用这里的 root segment。修改 key 结构时同步更新事件路由表。

export const qk = {
  health: () => ['health'] as const,
  ready: () => ['ready'] as const,
  runtime: () => ['runtime'] as const,
  workers: () => ['workers'] as const,
  metrics: () => ['metrics'] as const,
  latency: (params: Record<string, unknown>) => ['metrics', 'latency', params] as const,

  decisions: {
    list: (params: Record<string, unknown>) => ['decisions', 'list', params] as const,
    detail: (recordId: string) => ['decisions', 'detail', recordId] as const,
  },

  markets: {
    list: (params: Record<string, unknown>) => ['markets', 'list', params] as const,
    detail: (params: Record<string, unknown>) => ['markets', 'detail', params] as const,
    orderbook: (tokenId: string) => ['markets', 'orderbook', tokenId] as const,
    midpoint: (tokenId: string) => ['markets', 'midpoint', tokenId] as const,
    orderbookHistory: (params: Record<string, unknown>) =>
      ['markets', 'orderbook-history', params] as const,
    pricesHistory: (params: Record<string, unknown>) =>
      ['markets', 'prices-history', params] as const,
    settlement: (conditionId: string) => ['markets', 'settlement', conditionId] as const,
    settlements: (params: Record<string, unknown>) =>
      ['markets', 'settlements', params] as const,
  },

  orders: {
    list: (params: Record<string, unknown>) => ['orders', 'list', params] as const,
  },
  positions: {
    list: (params: Record<string, unknown>) => ['positions', 'list', params] as const,
  },
  fills: {
    list: (params: Record<string, unknown>) => ['fills', 'list', params] as const,
  },
  allocations: {
    list: (params: Record<string, unknown>) => ['allocations', 'list', params] as const,
    decisions: (params: Record<string, unknown>) =>
      ['allocations', 'decisions', params] as const,
  },
  auditEvents: {
    list: (params: Record<string, unknown>) => ['audit-events', 'list', params] as const,
    operators: (params: Record<string, unknown>) =>
      ['audit-events', 'operators', params] as const,
  },

  portfolio: {
    snapshot: () => ['portfolio', 'snapshot'] as const,
    equity: (params: Record<string, unknown>) => ['portfolio', 'equity', params] as const,
    pnlBreakdown: (params: Record<string, unknown>) =>
      ['portfolio', 'pnl-breakdown', params] as const,
    riskMetrics: (params: Record<string, unknown>) =>
      ['portfolio', 'risk-metrics', params] as const,
  },

  candidates: {
    list: (params: Record<string, unknown>) => ['candidates', 'list', params] as const,
    liveStates: (params: Record<string, unknown>) => ['candidates', 'live-states', params] as const,
    liveSourceGaps: (params: Record<string, unknown>) =>
      ['candidates', 'live-source-gaps', params] as const,
  },

  analytics: {
    funnel: (params: Record<string, unknown>) => ['analytics', 'funnel', params] as const,
    rejections: (params: Record<string, unknown>) => ['analytics', 'rejections', params] as const,
    executionQuality: (params: Record<string, unknown>) =>
      ['analytics', 'execution-quality', params] as const,
    edgeRealization: (params: Record<string, unknown>) =>
      ['analytics', 'edge-realization', params] as const,
    riskRejections: (params: Record<string, unknown>) =>
      ['analytics', 'risk-rejections', params] as const,
    riskRejectionsAggregate: (params: Record<string, unknown>) =>
      ['analytics', 'risk-rejections', 'aggregate', params] as const,
    calibration: (params: Record<string, unknown>) =>
      ['analytics', 'calibration', params] as const,
    missedOpportunities: (params: Record<string, unknown>) =>
      ['analytics', 'missed-opportunities', params] as const,
  },

  sports: {
    liveEvents: (params: Record<string, unknown>) => ['sports', 'live-events', params] as const,
  },

  parameters: {
    list: () => ['parameters', 'list'] as const,
    overrides: () => ['parameters', 'overrides'] as const,
    history: (params: Record<string, unknown>) => ['parameters', 'history', params] as const,
  },

  outbox: {
    pending: (params: Record<string, unknown>) => ['outbox', 'pending', params] as const,
    failures: (params: Record<string, unknown>) => ['outbox', 'failures', params] as const,
  },

  operations: {
    reconcileDiffs: (params: Record<string, unknown>) =>
      ['operations', 'reconcile-diffs', params] as const,
    /** parameter sweep 是 POST + 用户输入驱动；query key 只为 isFetching 状态用。 */
    parameterSweep: (params: Record<string, unknown>) =>
      ['operations', 'parameter-sweep', params] as const,
  },

  tradeReplays: {
    list: (params: Record<string, unknown>) => ['trade-replays', 'list', params] as const,
  },

  trades: {
    timeline: (conditionId: string, params: Record<string, unknown>) =>
      ['trades', 'timeline', conditionId, params] as const,
  },

} as const

// 顶层 key 段——SSE 事件路由按段精准 invalidate；不直接命中 query factory 上的私有签名。
export const qkRoots = {
  health: ['health'] as const,
  ready: ['ready'] as const,
  runtime: ['runtime'] as const,
  workers: ['workers'] as const,
  metrics: ['metrics'] as const,
  decisions: ['decisions'] as const,
  markets: ['markets'] as const,
  orders: ['orders'] as const,
  positions: ['positions'] as const,
  fills: ['fills'] as const,
  allocations: ['allocations'] as const,
  auditEvents: ['audit-events'] as const,
  portfolio: ['portfolio'] as const,
  candidates: ['candidates'] as const,
  analytics: ['analytics'] as const,
  outbox: ['outbox'] as const,
  operations: ['operations'] as const,
  tradeReplays: ['trade-replays'] as const,
  trades: ['trades'] as const,
  sports: ['sports'] as const,
  parameters: ['parameters'] as const,
} as const
