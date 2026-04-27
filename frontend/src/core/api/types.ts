export type JsonPrimitive = string | number | boolean | null
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue }
export type JsonObject = Record<string, JsonValue>

export interface PageResponse<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface BlockingIssue {
  field: string
  code: string
  message: string
  value?: JsonValue
}

export interface RuntimeStatus {
  phase: string
  ready_to_trade: boolean
  automatic_trading_enabled?: boolean
  user_ws_connected: boolean
  allow_new_entries: boolean
  last_reconcile_at: string | null
  portfolio_budget_usdc?: string | null
  queue_depth?: JsonValue
  queue_depths?: JsonValue
  persistence?: JsonValue
  blocking_reasons?: string[]
  warnings?: string[]
}

export interface ReadyPayload {
  ready_to_trade: boolean
  phase: string
  blocking_issues: BlockingIssue[]
  warnings: BlockingIssue[]
  runtime: RuntimeStatus
}

export interface RuntimePayload {
  phase: string
  ready_to_trade: boolean
  readiness: {
    ready_to_trade?: boolean
    phase?: string
    blocking_issues?: BlockingIssue[]
    warnings?: BlockingIssue[]
  }
  settings: JsonObject
  identity: {
    wallet_address: string | null
    funder_address: string | null
    signature_type: number | null
    profile_address: string | null
    profile_name: string | null
    profile_pseudonym: string | null
    profile_image: string | null
    profile_verified: boolean | null
    profile_x_username: string | null
  }
  runtime: RuntimeStatus
  bootstrap_summary: JsonValue
  market_discovery: MarketDiscoverySnapshot
  sports_live_sync?: JsonValue
  registry: {
    market_count: number
    markets: JsonValue[]
  }
  account: JsonObject
  event_bus: JsonValue
  persistence: JsonValue
  markets: MarketView[]
  portfolio: JsonObject
}

export interface MarketDiscoverySnapshot {
  round_id: number
  cursor_active: boolean
  round_started_at: string | null
  last_round_completed_at: string | null
  pages_scanned_in_round: number
  markets_seen_in_round: number
  last_completed_round_pages: number
  last_completed_round_markets: number
  last_page_size: number
  last_tick_started_at: string | null
  last_tick_completed_at: string | null
  last_tick_requests: number
  last_tick_markets: number
  last_error: string | null
  consecutive_failures: number
}

export interface PortfolioSnapshot {
  balance_usdc: string | null
  allowance_usdc: string | null
  available_usdc: string | null
  position_count: number
  open_order_count: number
  fill_count: number
  pause_count: number
  last_reconcile_at: string | null
  user_ws_connected: boolean
  allow_new_entries: boolean
  markets_tracked: number
  recent_allocations: AllocationRecord[]
}

export interface WorkerSnapshot {
  name?: string
  worker_name?: string
  status?: string
  state?: string
  healthy?: boolean
  priority?: string
  detail?: string
  last_heartbeat_at?: string | null
  last_error?: string | null
}

export interface WorkersPayload {
  phase: string
  automatic_trading_enabled: boolean
  queue_depths: JsonValue
  scheduler: JsonValue
  sports_live_sync?: JsonValue
  workers: WorkerSnapshot[]
}

export interface MetricsPayload {
  phase: string
  automatic_trading_enabled: boolean
  queue_depths: JsonValue
  metrics: JsonValue
  sports_live_sync?: JsonValue
}

export interface MarketSummary {
  condition_id: string
  market_slug: string
  event_slug: string | null
  event_id: string | null
  event_title: string | null
  token_ids: string[]
  outcomes: MarketOutcomeSummary[]
  icon_url: string | null
  end_date: string | null
  tick_size: string | null
  min_order_size: string | null
  neg_risk: boolean
  fees: {
    enabled: boolean
    maker_base_fee_bps: number | null
    taker_base_fee_bps: number | null
    fee_rate_bps: number | null
    fee_rate_updated_at: string | null
  }
  category: string | null
  tags: string[]
  matched_keywords: string[]
  trading_status: string
  reject_reason: string | null
}

export interface MarketOutcomeSummary {
  token_id: string
  outcome: string
}

export interface OrderbookLevel {
  price: string | null
  size: string | null
}

export interface OrderbookSnapshot {
  token_id: string
  condition_id: string | null
  market_slug: string | null
  best_bid: string | null
  best_ask: string | null
  best_bid_size: string | null
  best_ask_size: string | null
  last_trade_price: string | null
  tick_size: string | null
  spread: string | null
  received_at: string | null
  bids: OrderbookLevel[]
  asks: OrderbookLevel[]
}

export interface PositionRecord {
  condition_id: string
  token_id: string
  market_slug: string | null
  event_slug: string | null
  shares: string | null
  cost_usdc: string | null
  open_buy_shares: string | null
  open_sell_shares: string | null
  pending_buy_shares: string | null
  confirmed_shares: string | null
  last_order_id: string | null
  last_trade_id: string | null
  confirmation_status: string | null
  updated_at: string | null
  avg_price: string | null
  initial_value: string | null
  current_value: string | null
  cash_pnl: string | null
  percent_pnl: string | null
  realized_pnl: string | null
  percent_realized_pnl: string | null
  cur_price: string | null
  redeemable: boolean | null
}

export interface OrderRecord {
  trace_id: string | null
  condition_id: string
  token_id: string
  market_slug: string | null
  event_slug: string | null
  side: string
  order_type: string
  price: string | null
  amount_usdc: string | null
  size_shares: string | null
  filled_shares: string | null
  remaining_shares: string | null
  notional_usdc: string | null
  order_id: string | null
  trade_id: string | null
  status: string
  idempotency_key: string | null
  reason: string | null
  post_only: boolean
  created_at: string | null
  updated_at: string | null
}

export interface FillRecord {
  trace_id: string | null
  event_type: string
  event_id: string | null
  market_slug: string | null
  event_slug: string | null
  condition_id: string
  token_id: string
  reason: string | null
  created_at: string | null
  order_id: string | null
  trade_id: string | null
  side: string | null
  price: string | null
  size: string | null
  notional_usdc: string | null
  status: string | null
  confirmed_at: string | null
}

export interface TradeReplayRecord {
  condition_id: string
  token_id: string
  market_slug: string | null
  event_slug: string | null
  event_title: string | null
  outcome: string | null
  market_status: string | null
  settlement_status: string
  trace_ids: string[]
  order_ids: string[]
  trade_ids: string[]
  buy: {
    count: number
    size: string | null
    notional_usdc: string | null
    avg_price: string | null
  }
  sell: {
    count: number
    size: string | null
    notional_usdc: string | null
    avg_price: string | null
  }
  position: PositionRecord | null
  pnl: {
    source: string
    realized_pnl_usdc: string | null
    cash_pnl_usdc: string | null
    current_value_usdc: string | null
    initial_value_usdc: string | null
    open_cost_usdc: string | null
    net_cashflow_usdc: string | null
    avg_buy_price: string | null
    cur_price: string | null
    redeemable: boolean | null
    warning: string | null
    data_sources: string[]
  }
  sports_tail_game: JsonValue
  sports_live_match: JsonValue
  exit_plan: JsonValue
  candidate_reasons: string[]
  first_fill_at: string | null
  last_fill_at: string | null
  last_position_updated_at: string | null
  source_counts: JsonObject
}

export interface CandidateRecord {
  candidate_id?: string | null
  trace_id?: string | null
  condition_id?: string | null
  market_slug?: string | null
  event_slug?: string | null
  event_title?: string | null
  token_id: string
  outcome?: string | null
  ready_to_trade?: boolean
  accepted?: boolean
  action?: string | null
  execution_permission?: string | null
  league?: string | null
  home_name?: string | null
  away_name?: string | null
  period?: string | null
  observed_at?: string | null
  reason?: string | null
  market_type?: string | null
  side?: string | null
  line?: string | null
  best_ask?: string | null
  total_score?: number | string | null
  seconds_remaining?: number | string | null
  game_status?: string | null
  sports_risk_reason?: string | null
  exit_plan?: JsonValue
  confirmable: boolean
  allocation?: JsonValue
  intent?: JsonValue
  payload?: JsonValue
  created_at?: string | null
  updated_at?: string | null
}

export interface SportsLiveStateRecord {
  condition_id?: string | null
  market_slug?: string | null
  event_slug?: string | null
  metadata?: JsonObject
  source?: string | null
  updated_at?: string | null
}

export interface ConfirmCandidateRequest {
  condition_id?: string
  market_slug?: string
  token_id: string
  operator?: string
  note?: string
  trace_id?: string
}

export interface ConfirmCandidateResult {
  trace_id?: string | null
  status: string
  reason?: string | null
  condition_id?: string | null
  market_slug?: string | null
  token_id?: string | null
  order_id?: string | null
  trade_id?: string | null
  candidate?: CandidateRecord | null
  review?: JsonValue
  payload?: JsonValue
  raw_response?: JsonValue
}

export interface AuditEventRecord {
  trace_id: string | null
  event_id: string | null
  event_title: string
  market_slug: string | null
  event_slug: string | null
  condition_id: string | null
  token_id: string | null
  outcome: string | null
  side: string | null
  order_type: string | null
  price: JsonValue
  size: JsonValue
  notional_usdc: JsonValue
  order_id: string | null
  trade_id: string | null
  tx_hash: string | null
  status: string | null
  reason: string | null
  raw_response: JsonValue
  payload: JsonValue
  created_at: string | null
  updated_at: string | null
}

export interface OutboxEventRecord {
  trace_id: string | null
  event_type: string
  idempotency_key: string | null
  event_id: string | null
  market_slug: string | null
  event_slug: string | null
  condition_id: string | null
  token_id: string | null
  reason: string | null
  created_at: string | null
  priority: number | null
  retry_count: number | null
  last_error: string | null
  raw_response_summary: string | null
  payload: JsonValue
}

export interface AllocationRecord {
  condition_id: string
  market_slug: string | null
  event_slug: string | null
  token_id: string | null
  target_budget_usdc: string | null
  buy_budget_usdc: string | null
  current_exposure_usdc: string | null
  released_budget_usdc: string | null
  reason: string | null
  release_reason: string | null
  idempotency_key: string | null
}

export interface FeePreviewQuote {
  price: string | null
  price_source: string | null
  fee_usdc: string | null
  fee_shares: string | null
  charged_in: 'shares' | 'usdc'
}

export interface TakerFeePreview {
  basis_size_shares: string | null
  fee_rate_bps: number
  buy: FeePreviewQuote | null
  sell: FeePreviewQuote | null
}

export interface MarketView {
  market: MarketSummary
  tracked: boolean
  token_views: MarketTokenView[]
}

export interface MarketTokenView {
  token_id: string
  outcome: string
  orderbook: OrderbookSnapshot | null
  position: PositionRecord | null
  open_orders: OrderRecord[]
  open_order_count: number
  best_ask: string | null
  best_bid: string | null
  spread: string | null
  fee_preview: TakerFeePreview | null
}

export interface MidpointPayload {
  token_id: string
  condition_id: string | null
  market_slug: string | null
  source: string
  midpoint: string | null
  best_bid: string | null
  best_ask: string | null
  last_trade_price: string | null
  spread: string | null
  received_at: string | null
}

export interface PricesHistoryPayload {
  token_id: string
  interval: string | null
  fidelity: number | null
  history: Array<{
    timestamp: string
    price: string | null
  }>
}

export interface ReconcileResult {
  trace_id: string
  status: string
  reason?: string
  plan?: JsonValue
  actions?: JsonValue
  created_orders?: JsonValue
  cancelled_orders?: JsonValue
  skipped_orders?: JsonValue
}

export interface CancelReplaceSellPayload {
  status: string
  trace_id: string
  reason?: string
  operator?: string
  market?: JsonValue
  cancelled_orders?: JsonValue
  failed_cancels?: JsonValue
  replace_review?: JsonValue
  replace_order_submitted?: JsonValue
}
