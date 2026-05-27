import { apiClient } from './client'
import type {
  AllocationDecisionsPage,
  AllocationsPage,
  AuditEventsPage,
  BulkCancelResult,
  CalibrationSnapshot,
  Candidate,
  CandidatesPage,
  DataFreshnessSnapshot,
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
  LiveStateRow,
  LiveStatesPage,
  MarketImpact,
  MarketLiquidity,
  MarketSettlement,
  MarketSettlementsPage,
  MarketView,
  MarketsPage,
  Midpoint,
  MissedOpportunitiesSnapshot,
  OperatorInterventionsAggregate,
  Orderbook,
  OrdersPage,
  OutboxFailuresPage,
  OutboxPendingPage,
  OutboxQueueDepth,
  ParameterClearRequest,
  ParameterClearResult,
  ParameterOverride,
  ParameterOverridesList,
  ParameterSetRequest,
  ParametersRegistry,
  ParameterSweepRequest,
  ParameterSweepResponse,
  SweepParamSpec,
  PnlBreakdown,
  PnlBreakdownGroupBy,
  PortfolioExposure,
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
  healthWs: (signal?: AbortSignal) =>
    apiClient.get<HealthWsSnapshot>('/health/ws', { signal }),
  // `/metrics` 是 Prometheus exposition text/plain。dashboard 想看的
  // sse_active_subscribers / sse_dropped_events_total 已经在 RuntimeSnapshot 上,
  // 直接走 healthApi.runtime;此 endpoint 保留供 Prometheus dump / 调试展示。
  metrics: (signal?: AbortSignal) =>
    apiClient.get<string>('/metrics', { signal, responseType: 'text' }),
  latencyPercentiles: (params: { window_ms?: number; sample_limit?: number }, signal?: AbortSignal) =>
    apiClient.get<LatencyPercentilesSnapshot>('/metrics/latency-percentiles', {
      params,
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
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<DecisionsPage>('/admin/decisions/dump', {
      params,
      signal,
    }),
  detail: (recordId: string, signal?: AbortSignal) =>
    apiClient.get<DecisionRecord>(`/decisions/${encodeURIComponent(recordId)}`, { signal }),
}

// ---------- 市场 ----------

export const marketsApi = {
  trackingBreakdown: (signal?: AbortSignal) =>
    apiClient.get<{
      available: boolean
      total_registry?: number
      total_ws_tracked_tokens?: number
      ws_subscribed_token_count?: number
      total_entry_metadata?: number
      by_live_phase?: Record<string, number>
    }>('/markets/tracking-breakdown', { signal }),
  liquidity: (
    params: { token_id: string; condition_id?: string; market_slug?: string; depth_ticks?: number },
    signal?: AbortSignal,
  ) => apiClient.get<MarketLiquidity>('/markets/liquidity', { params, signal }),
  impact: (
    params: { token_id: string; size_usdc: number; condition_id?: string; market_slug?: string },
    signal?: AbortSignal,
  ) => apiClient.get<MarketImpact>('/markets/impact', { params, signal }),
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
  ) => apiClient.get<MarketsPage>('/markets', { params, signal }),
  detail: (
    params: { market_slug?: string; condition_id?: string; token_id?: string },
    signal?: AbortSignal,
  ) => apiClient.get<MarketView>('/markets/detail', { params, signal }),
  orderbook: (
    params: { market_slug?: string; condition_id?: string; token_id: string },
    signal?: AbortSignal,
  ) => apiClient.get<Orderbook>('/markets/orderbook', { params, signal }),
  midpoint: (
    params: { market_slug?: string; condition_id?: string; token_id: string },
    signal?: AbortSignal,
  ) => apiClient.get<Midpoint>('/markets/midpoint', { params, signal }),
  pricesHistory: (
    params: { token_id: string; fidelity?: number } & (
      | { interval: string; start_ts?: number; end_ts?: number }
      | { interval?: never; start_ts: number; end_ts?: number }
    ),
    signal?: AbortSignal,
  ) =>
    apiClient.get<PricesHistory>('/markets/prices-history', {
      params,
      signal,
    }),
  pause: (body: { condition_id: string; reason?: string; operator: string; trace_id?: string }) =>
    apiClient.post<WriteOperationResult>('/markets/pause', { body }),
  resume: (body: { condition_id: string; operator: string; trace_id?: string }) =>
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
      params,
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
      status?: string
      since?: number
      until?: number
    },
    signal?: AbortSignal,
  ) => apiClient.get<OrdersPage>('/orders', { params, signal }),
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
  bulkCancel: (body: {
    order_ids: string[]
    operator?: string
    reason?: string
    trace_id?: string
  }) => apiClient.post<BulkCancelResult>('/orders/bulk-cancel', { body }),
}

export const positionsApi = {
  list: (
    params: {
      limit?: number
      offset?: number
      condition_id?: string
      token_id?: string
    },
    signal?: AbortSignal,
  ) => apiClient.get<PositionsPage>('/positions', { params, signal }),
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
    },
    signal?: AbortSignal,
  ) => apiClient.get<FillsPage>('/fills', { params, signal }),
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
    },
    signal?: AbortSignal,
  ) => apiClient.get<AllocationsPage>('/allocations', { params, signal }),
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
      params,
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
    },
    signal?: AbortSignal,
  ) => apiClient.get<AuditEventsPage>('/audit-events', { params, signal }),
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
      params,
      signal,
    }),
}

// ---------- Portfolio ----------

export const portfolioApi = {
  snapshot: (signal?: AbortSignal) =>
    apiClient.get<PortfolioSnapshot>('/portfolio', { signal }),
  equityCurve: (params: { window_ms?: number; interval_ms?: number }, signal?: AbortSignal) =>
    apiClient.get<EquityCurve>('/portfolio/equity-curve', { params, signal }),
  pnlBreakdown: (
    params: {
      group_by: PnlBreakdownGroupBy
      condition_id?: string
      position_limit?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<PnlBreakdown>('/portfolio/pnl-breakdown', {
      params,
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
      params,
      signal,
    }),
  exposure: (signal?: AbortSignal) =>
    apiClient.get<PortfolioExposure>('/portfolio/exposure', { signal }),
}

// ---------- 候选 ----------

export const candidatesApi = {
  dataFreshness: (signal?: AbortSignal) =>
    apiClient.get<DataFreshnessSnapshot>('/candidates/data-freshness', { signal }),
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
    },
    signal?: AbortSignal,
  ) => apiClient.get<CandidatesPage>('/candidates', { params, signal }),
  liveStates: (params: { limit?: number; offset?: number }, signal?: AbortSignal) =>
    apiClient.get<LiveStatesPage>('/candidates/live-states', {
      params,
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
      params,
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
    },
    signal?: AbortSignal,
  ) => apiClient.get<FunnelSnapshot>('/analytics/funnel', { params, signal }),
  rejections: (
    params: {
      window_ms?: number
      end_ms?: number
      league?: string
      market_type?: string
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<RejectionsSnapshot>('/analytics/rejections', {
      params,
      signal,
    }),
  executionQuality: (
    params: {
      window_ms?: number
      end_ms?: number
      league?: string
      market_type?: string
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<ExecutionQualitySnapshot>('/analytics/execution-quality', {
      params,
      signal,
    }),
  edgeRealization: (
    params: {
      limit?: number
      condition_id?: string
      since?: number
      until?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<EdgeRealizationSnapshot>('/analytics/edge-realization', {
      params,
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
      params,
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
      params,
      signal,
    }),
  calibration: (
    params: {
      bucket_size?: number
      since?: number
      until?: number
      sample_limit?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<CalibrationSnapshot>('/analytics/calibration', {
      params,
      signal,
    }),
  missedOpportunities: (
    params: {
      limit?: number
      per_decision_usdc?: number
      since?: number
      until?: number
    },
    signal?: AbortSignal,
  ) =>
    apiClient.get<MissedOpportunitiesSnapshot>('/analytics/missed-opportunities', {
      params,
      signal,
    }),
  quantSummary: (
    params: { window_ms?: number } = {},
    signal?: AbortSignal,
  ) =>
    apiClient.get<QuantSummary>('/analytics/quant-summary', { params, signal }),
}

export type HealthWsSnapshot = {
  status: 'healthy' | 'degraded' | 'unhealthy'
  detail: {
    idle_threshold_s: number
    market_ws: {
      connected: boolean
      subscription_count: number
      last_message_at: string | null
      idle_seconds: number | null
      idle_checked: boolean
      idle_due_to_no_demand: boolean
      status: 'healthy' | 'degraded' | 'unhealthy'
    }
    user_ws: {
      connected: boolean
      subscription_count: number
      last_message_at: string | null
      idle_seconds: number | null
      idle_checked: boolean
      idle_due_to_no_demand: boolean
      status: 'healthy' | 'degraded' | 'unhealthy'
    }
  }
}

export type QuantSummary = {
  window_ms: number
  generated_at: string
  decision_counts: {
    total: number
    accepted: number
    rejected: number
    accept_rate_pct: number
  }
  rejection_top: { reason: string; count: number; pct: number }[]
  kelly_stats: {
    sample_count: number
    avg_prob_p: number | null
    avg_price_c: number | null
    avg_edge_net: number | null
    avg_f_star: number | null
    avg_buy_budget_usdc: number | null
    rounded_up_count: number
    rounded_up_pct: number
  }
  execution: {
    orders_submitted: number
    orders_filled: number
    orders_rejected: number
    fill_rate_pct: number
  }
  portfolio: {
    balance_usdc?: string
    net_value_usdc?: string
    cost_usdc?: string
    cash_pnl_usdc?: string
    position_count?: number
    open_order_count?: number
    paused_market_count?: number
    error?: string
  }
  market_coverage: {
    registry_total?: number
    with_live_state?: number
    signal_allowed?: number
    error?: string
  }
  signal_health: {
    ws_subscribed_tokens?: number
    user_ws_connected?: boolean
    last_reconcile_at?: string | null
    error?: string
  }
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
      params,
      signal,
    }),
  liveStates: (
    params: { limit?: number; offset?: number },
    signal?: AbortSignal,
  ) =>
    apiClient.get<LiveStatesPage>('/sports/live-states', {
      params,
      signal,
    }),
  liveStateForMarket: (conditionId: string, signal?: AbortSignal) =>
    apiClient.get<LiveStateRow>(`/sports/live-states/${encodeURIComponent(conditionId)}`, {
      signal,
    }),
  sourceGaps: (
    params: { limit?: number; offset?: number; prefix?: string },
    signal?: AbortSignal,
  ) =>
    apiClient.get<LiveSourceGapsPage>('/sports/source-gaps', {
      params,
      signal,
    }),
}

// ---------- Outbox ----------

export const outboxApi = {
  queueDepth: (signal?: AbortSignal) =>
    apiClient.get<OutboxQueueDepth>('/outbox/queue-depth', { signal }),
  pending: (
    params: { limit?: number; offset?: number; trace_id?: string },
    signal?: AbortSignal,
  ) => apiClient.get<OutboxPendingPage>('/outbox/pending', { params, signal }),
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
    apiClient.get<OutboxFailuresPage>('/outbox/failures', { params, signal }),
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
      params,
      signal,
    }),
  virtualPaperTrade: (body: {
    condition_id?: string
    token_id?: string
    market_slug?: string
  }) =>
    apiClient.post<VirtualPaperTradeResult>('/operations/virtual-paper-trade', { body }),
  pauseTrading: (body: { reason: string; operator: string; trace_id?: string }) =>
    apiClient.post<WriteOperationResult>('/operations/pause-trading', { body }),
  resumeTrading: (body: { operator: string; trace_id?: string }) =>
    apiClient.post<WriteOperationResult>('/operations/resume-trading', { body }),
  parameterSweepParams: (signal?: AbortSignal) =>
    apiClient.get<SweepParamSpec[]>('/operations/parameter-sweep/params', { signal }),
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
    },
    signal?: AbortSignal,
  ) => apiClient.get<TradeReplaysPage>('/trade-replays', { params, signal }),
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
      params,
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

