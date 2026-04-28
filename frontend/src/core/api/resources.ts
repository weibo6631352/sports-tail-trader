import { apiClient, buildSearch } from './client'
import type {
  AllocationRecord,
  AuditEventRecord,
  CancelReplaceSellPayload,
  CandidateRecord,
  ConfirmCandidateRequest,
  ConfirmCandidateResult,
  FillRecord,
  MarketView,
  MetricsPayload,
  MidpointPayload,
  OrderbookSnapshot,
  OrderRecord,
  OutboxEventRecord,
  PageResponse,
  PortfolioSnapshot,
  PositionRecord,
  PricesHistoryPayload,
  ReadyPayload,
  ReconcileResult,
  RuntimePayload,
  SportsLiveStateRecord,
  TradeReplayRecord,
  VirtualPaperTradeRequest,
  VirtualPaperTradeResult,
  WorkersPayload,
} from './types'

export const adminApi = {
  getReady: () => apiClient.get<ReadyPayload>('/ready'),
  getRuntime: () => apiClient.get<RuntimePayload>('/runtime'),
  getWorkers: () => apiClient.get<WorkersPayload>('/workers'),
  getMetrics: () => apiClient.get<MetricsPayload>('/metrics'),
  getPortfolio: () => apiClient.get<PortfolioSnapshot>('/portfolio'),
  listAllocations: (params: Record<string, unknown>) =>
    apiClient.get<PageResponse<AllocationRecord>>(`/allocations${buildSearch(params)}`),
  listMarkets: (params: Record<string, unknown>) =>
    apiClient.get<PageResponse<MarketView>>(`/markets${buildSearch(params)}`),
  getMarketDetail: (params: Record<string, unknown>) =>
    apiClient.get<MarketView>(`/markets/detail${buildSearch(params)}`),
  getMarketOrderbook: (params: Record<string, unknown>) =>
    apiClient.get<{
      token_id: string
      condition_id: string | null
      market_slug: string | null
      source: string
      orderbook: OrderbookSnapshot
    }>(`/markets/orderbook${buildSearch(params)}`),
  getMarketMidpoint: (params: Record<string, unknown>) =>
    apiClient.get<MidpointPayload>(`/markets/midpoint${buildSearch(params)}`),
  getMarketPricesHistory: (params: Record<string, unknown>) =>
    apiClient.get<PricesHistoryPayload>(`/markets/prices-history${buildSearch(params)}`),
  listOrders: (params: Record<string, unknown>) =>
    apiClient.get<PageResponse<OrderRecord>>(`/orders${buildSearch(params)}`),
  cancelReplaceSell: (payload: Record<string, unknown>) =>
    apiClient.post<CancelReplaceSellPayload>('/orders/cancel-replace-sell', payload),
  listPositions: (params: Record<string, unknown>) =>
    apiClient.get<PageResponse<PositionRecord>>(`/positions${buildSearch(params)}`),
  listFills: (params: Record<string, unknown>) =>
    apiClient.get<PageResponse<FillRecord>>(`/fills${buildSearch(params)}`),
  listTradeReplays: (params: Record<string, unknown>) =>
    apiClient.get<PageResponse<TradeReplayRecord>>(`/trade-replays${buildSearch(params)}`),
  listCandidates: (params: Record<string, unknown>) =>
    apiClient.get<PageResponse<CandidateRecord>>(`/candidates${buildSearch(params)}`),
  listSportsLiveStates: (params: Record<string, unknown>) =>
    apiClient.get<PageResponse<SportsLiveStateRecord>>(`/candidates/live-states${buildSearch(params)}`),
  confirmCandidate: (payload: ConfirmCandidateRequest) =>
    apiClient.post<ConfirmCandidateResult>('/candidates/confirm', payload),
  listAuditEvents: (params: Record<string, unknown>) =>
    apiClient.get<PageResponse<AuditEventRecord>>(`/audit-events${buildSearch(params)}`),
  listOutboxPending: (params: Record<string, unknown>) =>
    apiClient.get<PageResponse<OutboxEventRecord>>(`/outbox/pending${buildSearch(params)}`),
  reconcile: (payload: { trace_id?: string; condition_ids: string[] }) =>
    apiClient.post<ReconcileResult>('/operations/reconcile', payload),
  runVirtualPaperTrade: (payload: VirtualPaperTradeRequest) =>
    apiClient.post<VirtualPaperTradeResult>('/operations/virtual-paper-trade', payload),
}
