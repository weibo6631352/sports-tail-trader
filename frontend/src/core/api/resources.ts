import { apiClient } from './client'
import type {
  AllocationDecisionsPage,
  AllocationsPage,
  AuditEventsPage,
  CalibrationSnapshot,
  Candidate,
  CandidatesPage,
  DecisionRecord,
  DecisionsPage,
  EdgeRealizationSnapshot,
  EquityCurve,
  ExecutionQualitySnapshot,
  FillsPage,
  FunnelSnapshot,
  HealthSnapshot,
  LatencyPercentilesSnapshot,
  LiveSourceGapsPage,
  LiveStatesPage,
  MarketSettlement,
  MarketSettlementsPage,
  MarketView,
  MarketsPage,
  Midpoint,
  MissedOpportunitiesSnapshot,
  OperatorInterventionsAggregate,
  Orderbook,
  OrderbookHistoryPage,
  OrdersPage,
  OutboxFailuresPage,
  OutboxPendingPage,
  ParameterClearRequest,
  ParameterClearResult,
  ParameterOverride,
  ParameterOverridesList,
  ParameterSetRequest,
  ParametersRegistry,
  ParameterSweepRequest,
  ParameterSweepResponse,
  PnlBreakdown,
  PnlBreakdownGroupBy,
  PortfolioRiskMetrics,
  PortfolioSnapshot,
  PositionsPage,
  PricesHistory,
  ReadinessSnapshot,
  ReconcileDiffsPage,
  RejectionsSnapshot,
  RiskRejectionAggregate,
  RiskRejectionsPage,
  RuntimeSnapshot,
  SettleMarketRequest,
  SportsLiveEventsPage,
  TradeReplaysPage,
  TradeTimeline,
  VirtualPaperTradeResult,
  WorkersSnapshot,
  WriteOperationResult,
} from './types'
import type { MetricsSnapshot, QueryParams } from './types'

// 所有 resource 函数走 apiClient 的对象参数签名 { params?, body?, signal? }。
// GET 函数额外接收末位 signal?:AbortSignal——由 React Query 的 queryFn
// ({ signal }) => api.foo.list(params, signal) 传入，切页 / unmount 时浏览器
// 自动 abort 在飞请求，省后端 cycle、省前端处理已废弃响应。
// POST/PUT/DELETE 是用户主动 mutation，不接 signal（用户既然点了就让它完成）。

// ---------- 健康 / 运行时 ----------

export const healthApi = {
  health: (signal?: AbortSignal) => apiClient.get<HealthSnapshot>('/health', { signal }),
  ready: (signal?: AbortSignal) => apiClient.get<ReadinessSnapshot>('/ready', { signal }),
  runtime: (signal?: AbortSignal) => apiClient.get<RuntimeSnapshot>('/runtime', { signal }),
  workers: (signal?: AbortSignal) => apiClient.get<WorkersSnapshot>('/workers', { signal }),
  metrics: (signal?: AbortSignal) => apiClient.get<MetricsSnapshot>('/metrics', { signal }),
  latencyPercentiles: (params: { window_ms?: number; sample_limit?: number }, signal?: AbortSignal) =>
    apiClient.get<LatencyPercentilesSnapshot>('/metrics/latency-percentiles', {
      params: params as QueryParams,
      signal,
    }),
}

// ---------- 决策 ----------

export const decisionsApi = {
  list: (
    params: {
      limit?: number
      offset?: number
      trace_id?: string
      condition_id?: string
      accepted?: boolean
      since?: number
      until?: number
      strategy_id?: string
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<DecisionsPage>('/admin/decisions/dump', {
      params: params as QueryParams,
      signal,
    }),
  detail: (recordId: string, signal?: AbortSignal) =>
    apiClient.get<DecisionRecord>(`/decisions/${encodeURIComponent(recordId)}`, { signal }),
}

// ---------- 市场 ----------

export const marketsApi = {
  list: (
    params: {
      limit?: number
      offset?: number
      trading_status?: string
      fees_enabled?: boolean
      fee_rate_bps_min?: number
      fee_rate_bps_max?: number
      maker_base_fee_bps_min?: number
      maker_base_fee_bps_max?: number
      taker_base_fee_bps_min?: number
      taker_base_fee_bps_max?: number
      sort_by?: string
      sort_direction?: 'asc' | 'desc'
    },
    signal?: AbortSignal,
  ) => apiClient.get<MarketsPage>('/markets', { params: params as QueryParams, signal }),
  detail: (
    params: { market_slug?: string; condition_id?: string; token_id?: string },
    signal?: AbortSignal,
  ) => apiClient.get<MarketView>('/markets/detail', { params: params as QueryParams, signal }),
  orderbook: (
    params: { market_slug?: string; condition_id?: string; token_id: string },
    signal?: AbortSignal,
  ) => apiClient.get<Orderbook>('/markets/orderbook', { params: params as QueryParams, signal }),
  midpoint: (
    params: { market_slug?: string; condition_id?: string; token_id: string },
    signal?: AbortSignal,
  ) => apiClient.get<Midpoint>('/markets/midpoint', { params: params as QueryParams, signal }),
  orderbookHistory: (
    params: {
      limit?: number
      offset?: number
      token_id?: string
      condition_id?: string
      since?: number
      until?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<OrderbookHistoryPage>('/markets/orderbook-history', {
      params: params as QueryParams,
      signal,
    }),
  pricesHistory: (
    params: {
      token_id: string
      start_ts?: number
      end_ts?: number
      interval?: string
      fidelity?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<PricesHistory>('/markets/prices-history', {
      params: params as QueryParams,
      signal,
    }),
  pause: (body: { condition_id: string; reason?: string; operator: string }) =>
    apiClient.post<WriteOperationResult>('/markets/pause', { body }),
  resume: (body: { condition_id: string; operator: string }) =>
    apiClient.post<WriteOperationResult>('/markets/resume', { body }),
  settlement: (conditionId: string, signal?: AbortSignal) =>
    apiClient.get<MarketSettlement>(`/markets/${encodeURIComponent(conditionId)}/settlement`, {
      signal,
    }),
  settlements: (
    params: {
      limit?: number
      offset?: number
      condition_id?: string
      since?: number
      until?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<MarketSettlementsPage>('/markets/settlements', {
      params: params as QueryParams,
      signal,
    }),
  settle: (body: SettleMarketRequest) =>
    apiClient.post<WriteOperationResult>('/markets/settle', { body }),
}

// ---------- 订单 / 持仓 / 成交 / 资金分配 / 审计 ----------

export const ordersApi = {
  list: (
    params: {
      limit?: number
      offset?: number
      open_only?: boolean
      condition_id?: string
      token_id?: string
      trace_id?: string
      order_id?: string
      trade_id?: string
      since?: number
      until?: number
      strategy_id?: string
    },
    signal?: AbortSignal,
  ) => apiClient.get<OrdersPage>('/orders', { params: params as QueryParams, signal }),
  replace: (body: {
    order_id: string
    new_price: string
    size_shares?: string
    operator: string
    reason: string
    trace_id?: string
    market_slug?: string
    condition_id?: string
    token_id?: string
  }) => apiClient.post<WriteOperationResult>('/orders/replace', { body }),
  cancel: (body: {
    order_id: string
    operator: string
    reason: string
    trace_id?: string
    market_slug?: string
    condition_id?: string
    token_id?: string
  }) => apiClient.post<WriteOperationResult>('/orders/cancel', { body }),
}

export const positionsApi = {
  list: (
    params: {
      limit?: number
      offset?: number
      condition_id?: string
      token_id?: string
      strategy_id?: string
    },
    signal?: AbortSignal,
  ) => apiClient.get<PositionsPage>('/positions', { params: params as QueryParams, signal }),
  forceExit: (body: {
    condition_id: string
    token_id: string
    price?: string
    operator: string
    reason: string
    trace_id?: string
  }) => apiClient.post<WriteOperationResult>('/positions/force-exit', { body }),
}

export const fillsApi = {
  list: (
    params: {
      limit?: number
      offset?: number
      trace_id?: string
      order_id?: string
      trade_id?: string
      condition_id?: string
      token_id?: string
      since?: number
      until?: number
      strategy_id?: string
    },
    signal?: AbortSignal,
  ) => apiClient.get<FillsPage>('/fills', { params: params as QueryParams, signal }),
}

export const allocationsApi = {
  list: (
    params: {
      limit?: number
      offset?: number
      trace_id?: string
      condition_id?: string
      token_id?: string
      market_slug?: string
      strategy_id?: string
    },
    signal?: AbortSignal,
  ) => apiClient.get<AllocationsPage>('/allocations', { params: params as QueryParams, signal }),
  decisions: (
    params: {
      limit?: number
      offset?: number
      condition_id?: string
      since?: number
      until?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<AllocationDecisionsPage>('/allocations/decisions', {
      params: params as QueryParams,
      signal,
    }),
}

export const auditEventsApi = {
  list: (
    params: {
      limit?: number
      offset?: number
      trace_id?: string
      event_title?: string
      condition_id?: string
      token_id?: string
      since?: number
      until?: number
      strategy_id?: string
    },
    signal?: AbortSignal,
  ) => apiClient.get<AuditEventsPage>('/audit-events', { params: params as QueryParams, signal }),
  operators: (
    params: {
      operator?: string
      since?: number
      until?: number
      sample_limit?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<OperatorInterventionsAggregate>('/audit-events/operators', {
      params: params as QueryParams,
      signal,
    }),
}

// ---------- Portfolio ----------

export const portfolioApi = {
  snapshot: (signal?: AbortSignal) =>
    apiClient.get<PortfolioSnapshot>('/portfolio', { signal }),
  equityCurve: (params: { window_ms?: number; interval_ms?: number }, signal?: AbortSignal) =>
    apiClient.get<EquityCurve>('/portfolio/equity-curve', { params: params as QueryParams, signal }),
  pnlBreakdown: (
    params: {
      group_by: PnlBreakdownGroupBy
      strategy_id?: string
      condition_id?: string
      position_limit?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<PnlBreakdown>('/portfolio/pnl-breakdown', {
      params: params as QueryParams,
      signal,
    }),
  riskMetrics: (
    params: {
      window_ms?: number
      interval_ms?: number
      annualization_factor?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<PortfolioRiskMetrics>('/portfolio/risk-metrics', {
      params: params as QueryParams,
      signal,
    }),
}

// ---------- 候选 ----------

export const candidatesApi = {
  list: (
    params: {
      limit?: number
      offset?: number
      condition_id?: string
      token_id?: string
      market_slug?: string
      market_type?: string
      game_status?: string
      action?: string
      execution_permission?: string
      accepted?: boolean
      confirmable?: boolean
      league?: string
      strategy_id?: string
    },
    signal?: AbortSignal,
  ) => apiClient.get<CandidatesPage>('/candidates', { params: params as QueryParams, signal }),
  liveStates: (params: { limit?: number; offset?: number }, signal?: AbortSignal) =>
    apiClient.get<LiveStatesPage>('/candidates/live-states', {
      params: params as QueryParams,
      signal,
    }),
  liveSourceGaps: (
    params: {
      limit?: number
      offset?: number
      prefix?: string
      include_future_schedule?: boolean
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<LiveSourceGapsPage>('/candidates/live-source-gaps', {
      params: params as QueryParams,
      signal,
    }),
  upsertLiveState: (body: {
    payload: Record<string, unknown>
    signal_allowed?: boolean | null
    signal_reason?: string
    condition_id?: string
    market_slug?: string
    event_slug?: string
    source?: string
  }) => apiClient.post<WriteOperationResult>('/candidates/live-states', { body }),
  confirm: (body: {
    token_id: string
    condition_id?: string
    market_slug?: string
    operator: string
    note?: string
    trace_id?: string
  }) =>
    apiClient.post<WriteOperationResult & { candidate?: Candidate }>('/candidates/confirm', {
      body,
    }),
}

// ---------- Analytics ----------

export const analyticsApi = {
  funnel: (
    params: {
      window_ms?: number
      end_ms?: number
      league?: string
      market_type?: string
      strategy_id?: string
    },
    signal?: AbortSignal,
  ) => apiClient.get<FunnelSnapshot>('/analytics/funnel', { params: params as QueryParams, signal }),
  rejections: (
    params: {
      window_ms?: number
      end_ms?: number
      league?: string
      market_type?: string
      strategy_id?: string
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<RejectionsSnapshot>('/analytics/rejections', {
      params: params as QueryParams,
      signal,
    }),
  executionQuality: (
    params: {
      window_ms?: number
      end_ms?: number
      league?: string
      market_type?: string
      strategy_id?: string
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<ExecutionQualitySnapshot>('/analytics/execution-quality', {
      params: params as QueryParams,
      signal,
    }),
  edgeRealization: (
    params: {
      limit?: number
      strategy_id?: string
      condition_id?: string
      since?: number
      until?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<EdgeRealizationSnapshot>('/analytics/edge-realization', {
      params: params as QueryParams,
      signal,
    }),
  riskRejections: (
    params: {
      limit?: number
      offset?: number
      condition_id?: string
      since?: number
      until?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<RiskRejectionsPage>('/analytics/risk-rejections', {
      params: params as QueryParams,
      signal,
    }),
  riskRejectionsAggregate: (
    params: {
      sample_limit?: number
      condition_id?: string
      since?: number
      until?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<RiskRejectionAggregate>('/analytics/risk-rejections/aggregate', {
      params: params as QueryParams,
      signal,
    }),
  calibration: (
    params: {
      bucket_size?: number
      strategy_id?: string
      since?: number
      until?: number
      sample_limit?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<CalibrationSnapshot>('/analytics/calibration', {
      params: params as QueryParams,
      signal,
    }),
  missedOpportunities: (
    params: {
      limit?: number
      per_decision_usdc?: number
      strategy_id?: string
      since?: number
      until?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<MissedOpportunitiesSnapshot>('/analytics/missed-opportunities', {
      params: params as QueryParams,
      signal,
    }),
}

// ---------- Sports live events 历史 ----------

export const sportsApi = {
  liveEvents: (
    params: {
      limit?: number
      offset?: number
      condition_id?: string
      since?: number
      until?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<SportsLiveEventsPage>('/sports/live-events', {
      params: params as QueryParams,
      signal,
    }),
}

// ---------- Outbox ----------

export const outboxApi = {
  pending: (
    params: { limit?: number; offset?: number; trace_id?: string },
    signal?: AbortSignal,
  ) => apiClient.get<OutboxPendingPage>('/outbox/pending', { params: params as QueryParams, signal }),
  failures: (
    params: {
      limit?: number
      offset?: number
      trace_id?: string
      event_type?: string
      since?: number
      until?: number
      min_retry_count?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<OutboxFailuresPage>('/outbox/failures', { params: params as QueryParams, signal }),
}

// ---------- Operations ----------

export const operationsApi = {
  reconcile: (body: { trace_id?: string; condition_ids?: string[] }) =>
    apiClient.post<WriteOperationResult>('/operations/reconcile', { body }),
  reconcileDiffs: (
    params: {
      limit?: number
      offset?: number
      trace_id?: string
      condition_id?: string
      since?: number
      until?: number
      include_started?: boolean
      include_applied?: boolean
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<ReconcileDiffsPage>('/operations/reconcile/diffs', {
      params: params as QueryParams,
      signal,
    }),
  virtualPaperTrade: (body: {
    condition_id?: string
    token_id?: string
    market_slug?: string
  }) =>
    apiClient.post<VirtualPaperTradeResult>('/operations/virtual-paper-trade', { body }),
  pauseTrading: (body: { reason: string; operator: string }) =>
    apiClient.post<WriteOperationResult>('/operations/pause-trading', { body }),
  resumeTrading: (body: { operator: string }) =>
    apiClient.post<WriteOperationResult>('/operations/resume-trading', { body }),
  parameterSweep: (body: ParameterSweepRequest) =>
    apiClient.post<ParameterSweepResponse>('/operations/parameter-sweep', { body }),
}

// ---------- Trade replays & timeline ----------

export const tradeReplaysApi = {
  list: (
    params: {
      limit?: number
      offset?: number
      condition_id?: string
      token_id?: string
      trace_id?: string
      strategy_id?: string
    },
    signal?: AbortSignal,
  ) => apiClient.get<TradeReplaysPage>('/trade-replays', { params: params as QueryParams, signal }),
}

export const tradesApi = {
  timeline: (
    conditionId: string,
    params: {
      token_id?: string
      since?: number
      until?: number
      limit?: number
      per_table_limit?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<TradeTimeline>(`/trades/${encodeURIComponent(conditionId)}/timeline`, {
      params: params as QueryParams,
      signal,
    }),
}

// ---------- 实时调参 /parameters ----------
// 后端是 registry + scope/key 白名单。重启即丢；写入自动落审计。

export const parametersApi = {
  list: (signal?: AbortSignal) =>
    apiClient.get<ParametersRegistry>('/parameters', { signal }),
  overrides: (signal?: AbortSignal) =>
    apiClient.get<ParameterOverridesList>('/parameters/overrides', { signal }),
  set: (scope: string, key: string, body: ParameterSetRequest) =>
    apiClient.put<ParameterOverride>(
      `/parameters/${encodeURIComponent(scope)}/${encodeURIComponent(key)}`,
      { body },
    ),
  clear: (scope: string, key: string, body: ParameterClearRequest) =>
    apiClient.del<ParameterClearResult>(
      `/parameters/${encodeURIComponent(scope)}/${encodeURIComponent(key)}`,
      { body },
    ),
}

// ---------- 总入口 ----------

export const api = {
  health: healthApi,
  decisions: decisionsApi,
  markets: marketsApi,
  orders: ordersApi,
  positions: positionsApi,
  fills: fillsApi,
  allocations: allocationsApi,
  auditEvents: auditEventsApi,
  portfolio: portfolioApi,
  candidates: candidatesApi,
  analytics: analyticsApi,
  outbox: outboxApi,
  operations: operationsApi,
  tradeReplays: tradeReplaysApi,
  trades: tradesApi,
  sports: sportsApi,
  parameters: parametersApi,
}
