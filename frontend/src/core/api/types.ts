// 后端 Decimal 一律字符串化（serializer.decimal_text）；前端用 decimal.js 处理。
// 后端 datetime 一律 ISO 8601 UTC 字符串。
// 大量未结构化字段保持 unknown / Record<string, unknown>，UI 按需投影。

// 从 client.ts 导出 QueryParams（resources.ts 用），方便外部 import。
export type { QueryParams, RequestOptions } from './client'

export type Iso = string
export type DecimalStr = string

export type Page<T> = {
  items: T[]
  total?: number
  limit: number
  offset: number
  next_offset?: number | null
  has_more?: boolean
}

// ---------- 健康 / 就绪 ----------

export type HealthSnapshot = Record<string, string>

export type ReadinessSnapshot = {
  // /ready 顶层字段
  ready_to_trade?: boolean
  phase?: string
  blocking_reasons?: string[]
  blocking_issues?: Array<{ field: string; code: string; message: string }>
  warnings?: unknown[]
  runtime?: {
    user_ws_connected?: boolean
    [key: string]: unknown
  }
  // /runtime.readiness 子对象字段（market_ws_connected / trading_client_ready 等在此层）
  ready?: boolean
  live?: boolean
  automatic_trading_enabled?: boolean
  config_ready?: boolean
  db_ready?: boolean
  trading_client_ready?: boolean
  market_ws_connected?: boolean
  user_ws_connected?: boolean
  reconcile_fresh?: boolean
  trading_status?: string
  blocked_on?: string
  [key: string]: unknown
}

// ---------- 运行时 / Workers / Metrics ----------

export type RuntimeIdentity = {
  wallet_address?: string | null
  funder_address?: string | null
  signature_type?: number | null
  profile_address?: string | null
  profile_name?: string | null
  profile_pseudonym?: string | null
  // Polymarket gamma public-profile API 返回 `profileImage`,后端 normalize 为
  // profile_image —— 之前前端误用 profile_avatar 导致头像取不到。
  profile_image?: string | null
  profile_verified?: boolean | null
  profile_x_username?: string | null
}

export type RuntimeSettings = {
  paper_trading?: boolean
  [key: string]: unknown
}

export type RuntimeSnapshot = {
  settings: RuntimeSettings
  identity?: RuntimeIdentity
  readiness?: ReadinessSnapshot
  ready?: boolean
  automatic_trading_enabled?: boolean
  trading_status?: string
  phase?: string
  active_market_count?: number
  [key: string]: unknown
}

export type WorkerHealth = {
  name: string
  priority?: string
  state?: string
  healthy?: boolean
  last_error?: string | null
  last_heartbeat_at?: Iso | null
  queue_depth?: number
  [key: string]: unknown
}

export type SchedulerJob = {
  name: string
  priority?: string
  interval_seconds?: number | null
  enabled?: boolean
  paused?: boolean
  running?: boolean
  run_count?: number
  last_started_at?: Iso | null
  last_finished_at?: Iso | null
  last_duration_ms?: number | null
  next_run_at?: Iso | null
  last_error?: string | null
  tags?: string[]
}

export type WorkersSnapshot = {
  automatic_trading_enabled?: boolean
  workers?: WorkerHealth[]
  scheduler?: {
    jobs?: SchedulerJob[]
    created_at?: Iso
  }
  queue_depths?: Record<string, number>
  sse_active_subscribers?: number
  sse_dropped_events_total?: number
  [key: string]: unknown
}

export type MetricsSnapshot = {
  phase?: string
  automatic_trading_enabled?: boolean
  queue_depths?: unknown
  metrics?: {
    collected_at?: string
    queue_depths?: unknown[]
    ws_states?: unknown[]
    reconcile?: unknown
    trading_gate?: unknown
    counters?: Array<{ name: string; value: number; labels: unknown[]; updated_at: string }>
    gauges?: Array<{ name: string; value: number; labels: unknown[]; updated_at: string }>
    histograms?: unknown[]
    timestamps?: unknown[]
  }
  sports_live_sync?: unknown
  sse_active_subscribers?: number
  sse_dropped_events_total?: number
  // legacy fields (不再由后端返回，保留避免调用侧报错)
  orders_acked?: number
  fills_total?: number
  decisions_total?: number
  decisions_accepted?: number
  decisions_rejected?: number
  sse_subscriber_cap?: number
  [key: string]: unknown
}

export type LatencyStage = 'queue_to_sign' | 'sign_to_submit' | 'submit_to_ack' | 'queue_to_ack' | string

export type LatencyPercentile = {
  stage: LatencyStage
  sample_count: number
  p50_ms?: number
  p90_ms?: number
  p95_ms?: number
  p99_ms?: number
  min_ms?: number
  max_ms?: number
}

export type LatencyPercentilesSnapshot = {
  window_ms?: number | null
  sample_limit: number
  stages: LatencyPercentile[]
  generated_at?: Iso
  [key: string]: unknown
}

// ---------- 决策记录 ----------

// decision_output 嵌套 metadata.kelly 是操盘复盘核心:
// quant_decide 时点的 prob_p / edge / f_star / buy_budget 等 14 字段全留底.
export type DecisionOutput = {
  action?: string | null
  decision_kind?: string | null
  reason?: string | null
  price?: DecimalStr | null
  amount_usdc?: DecimalStr | null
  size_shares?: DecimalStr | null
  order_id?: string | null
  order_type?: string | null
  post_only?: boolean
  token_id?: string | null
  market_slug?: string | null
  summary?: Record<string, unknown> | null
  intent_tags?: string[]
  metadata?: {
    kelly?: KellyInternals
    signal_at?: Iso
    [key: string]: unknown
  }
  [key: string]: unknown
}

export type DecisionRecord = {
  record_id: string
  trace_id: string
  condition_id: string
  token_id?: string | null
  market_slug?: string | null
  hook_name?: string | null
  accepted: boolean
  reason?: string | null
  created_at: Iso
  decision_input?: Record<string, unknown>
  decision_output?: DecisionOutput
}

export type DecisionsPage = Page<DecisionRecord>

// ---------- 市场 ----------

// MarketOutcome (后端 /markets/detail outcomes[i]). 嵌套 position 子对象, 不
// 再用旧 position_size_shares / position_cost_usdc 顶层别名.
export type MarketOutcome = {
  token_id: string
  outcome?: string | null
  best_bid?: DecimalStr | null
  best_ask?: DecimalStr | null
  shares?: DecimalStr | null
  has_open_buy?: boolean
  has_open_sell?: boolean
  open_orders_count?: number
  position?: {
    shares?: DecimalStr | null
    cost_usdc?: DecimalStr | null
    cur_price?: DecimalStr | null
    redeemable?: boolean | null
  } | null
  [key: string]: unknown
}

export type FeePreview = {
  fee_rate_bps?: number | null
  fees_enabled?: boolean | null
  maker_base_fee_bps?: number | null
  taker_base_fee_bps?: number | null
  fee_rate_updated_at?: Iso | null
  fee_per_usdc?: DecimalStr | null
}

export type MarketView = {
  market_slug: string
  condition_id: string
  event_id?: string | null
  event_title?: string | null
  event_slug?: string | null
  icon_url?: string | null
  end_date?: Iso | null
  game_start_time?: Iso | null
  category?: string | null
  market_question?: string | null
  market_name?: string | null
  trading_status?: string | null
  sports_market_type?: string | null
  is_paused?: boolean
  has_any_position?: boolean
  total_position_usdc?: DecimalStr | null
  total_shares?: DecimalStr | null
  outcome_count?: number
  tick_size?: DecimalStr | null
  min_order_size?: DecimalStr | null
  neg_risk?: boolean
  outcomes?: MarketOutcome[]
  tags?: string[]
  matched_keywords?: string[]
  fee_preview?: FeePreview | null
  pause?: {
    source?: string
    reason?: string
    recoverable?: boolean
  } | null
  metadata?: Record<string, unknown> | null
  [key: string]: unknown
}

export type MarketsPage = Page<MarketView>

export type OrderbookLevel = { price: DecimalStr; size: DecimalStr }

export type Orderbook = {
  token_id: string
  bids: OrderbookLevel[]
  asks: OrderbookLevel[]
  midpoint?: DecimalStr | null
  spread?: DecimalStr | null
  fetched_at?: Iso
  [key: string]: unknown
}

export type Midpoint = {
  token_id: string
  midpoint: DecimalStr | null
  fetched_at?: Iso
}

export type PriceHistoryPoint = {
  t: number
  p: DecimalStr
  o?: DecimalStr
  h?: DecimalStr
  l?: DecimalStr
  c?: DecimalStr
}

export type PricesHistory = {
  token_id: string
  interval?: string | null
  fidelity?: number | null
  points: PriceHistoryPoint[]
}

// ---------- 订单 / 持仓 / 成交 / 资金分配 ----------

// 字段与后端 order_aggregator._serialize 完全对齐.
// 删 stale: signed_at/ack_at/operator (后端不返回, 走 audit_events 查 lifecycle).
// 补 backend 真实字段: amount_usdc/event_slug/idempotency_key/outcome/post_only.
export type OrderRow = {
  order_id: string
  trade_id?: string | null
  trace_id?: string | null
  idempotency_key?: string | null
  market_slug?: string | null
  event_slug?: string | null
  condition_id?: string | null
  token_id?: string | null
  outcome?: string | null
  side: 'BUY' | 'SELL' | string
  order_type?: string
  status: string
  price?: DecimalStr | null
  amount_usdc?: DecimalStr | null
  size_shares?: DecimalStr | null
  filled_shares?: DecimalStr | null
  remaining_shares?: DecimalStr | null
  notional_usdc?: DecimalStr | null
  post_only?: boolean
  reason?: string | null
  created_at: Iso
  updated_at?: Iso | null
  [key: string]: unknown
}

export type OrdersPage = Page<OrderRow>

// 字段名对齐后端 position_aggregator._serialize (level=summary).
// 后端返回的字段名是真相, 前端不再用旧 size_shares / current_price 等别名.
export type PositionRow = {
  condition_id: string
  token_id: string
  outcome?: string | null
  market_slug?: string | null
  event_title?: string | null
  shares: DecimalStr
  cost_usdc?: DecimalStr | null
  cur_price?: DecimalStr | null
  position_usdc?: DecimalStr | null
  best_bid?: DecimalStr | null
  best_ask?: DecimalStr | null
  has_open_buy?: boolean
  has_open_sell?: boolean
  redeemable?: boolean | null
  settled_zero_value?: boolean | null
  is_paused?: boolean
  // level=detail 才有的字段(/positions?level=detail):
  cash_pnl?: DecimalStr | null
  percent_pnl?: DecimalStr | null
  realized_pnl?: DecimalStr | null
  open_buy_shares?: DecimalStr | null
  open_sell_shares?: DecimalStr | null
  confirmation_status?: string | null
  updated_at?: Iso | null
  [key: string]: unknown
}

// 后端 /positions 包络 {positions, count}, 不是 Page<T>.
export type PositionsPage = {
  positions: PositionRow[]
  count: number
}

export type FillRow = {
  fill_id: string
  trace_id?: string | null
  order_id?: string | null
  trade_id?: string | null
  condition_id?: string | null
  token_id?: string | null
  side?: string | null
  price: DecimalStr
  size: DecimalStr
  notional_usdc?: DecimalStr | null
  fee_usdc?: DecimalStr | null
  event_type?: string | null
  created_at: Iso
  confirmed_at?: Iso | null
  [key: string]: unknown
}

export type FillsPage = Page<FillRow>

export type AllocationRow = {
  allocation_id: string
  trace_id?: string | null
  condition_id?: string | null
  token_id?: string | null
  market_slug?: string | null
  target_budget_usdc?: DecimalStr | null
  buy_budget_usdc?: DecimalStr | null
  reason?: string | null
  release_reason?: string | null
  created_at: Iso
  released_at?: Iso | null
  [key: string]: unknown
}

export type AllocationsPage = Page<AllocationRow>

export type AuditEventRow = {
  event_id: string
  trace_id?: string | null
  event_title: string
  status?: string | null
  reason?: string | null
  operator?: string | null
  condition_id?: string | null
  token_id?: string | null
  created_at: Iso
  payload?: Record<string, unknown>
  [key: string]: unknown
}

export type AuditEventsPage = Page<AuditEventRow>

// ---------- AuditEvent / OutboxEvent payload 收紧（按 event_type 做 narrow） ----------
// 后端契约：infra/outbox/event_sink.py::_FIELD_PROJECTIONS / _FUNCTION_PROJECTIONS
// 决定每种 event_type 落库时的字段白名单；audit_events.event_title 与
// outbox_events.event_type 同源（workers/persistence/records.py 用 event_type
// 字符串填 event_title）。所以两侧共用同一份 by-type schema。
//
// 这里只覆盖 dashboard / audit / strategy-config 等高频读取的 event_type；
// 其余事件 payload 仍按 base 类型 Record<string, unknown> 处理。新增 event 时
// 在 AuditEventPayloadByType 里加键即可，narrowAuditEvent / narrowOutboxEvent
// 自动覆盖。
//
// 注意：base AuditEventRow / OutboxPendingRow 不变，callsite 通过 narrowAuditEvent
// 主动 narrow；未识别的 event_type 返回 null，由 callsite 兜底（不抛错、不破坏列表渲染）。

/** 共享：order_cancel_requested / order_cancelled / replace_order_submitted。 */
export type OrderActionEventPayload = {
  operator?: string | null
  reason?: string | null
  order_id?: string | null
  trade_id?: string | null
  old_price?: DecimalStr | null
  new_price?: DecimalStr | null
  old_size_shares?: DecimalStr | null
  new_size_shares?: DecimalStr | null
  side?: 'BUY' | 'SELL' | string | null
  order_type?: string | null
  result_status?: string | null
  occurred_at?: Iso | null
}

/** 共享：trading_paused / trading_resumed。 */
export type TradingToggleEventPayload = {
  operator?: string | null
  reason?: string | null
  phase_before?: string | null
  phase_after?: string | null
  previous_manual_pause_reason?: string | null
  degraded_reason?: string | null
  occurred_at?: Iso | null
}

export type ParameterOverrideAppliedPayload = {
  scope: string
  key: string
  previous_value: unknown
  new_value: unknown
  operator: string
  applied_at: Iso
  expires_at: Iso | null
  cleared: boolean
}

export type RiskRejectionRecordedPayload = {
  passed?: boolean
  reason?: string | null
  failed_field?: string | null
  checks?: RiskCheck[]
  intent_summary?: Record<string, unknown>
  decision_kind?: string | null
}

export type AllocationDecisionRecordedPayload = {
  candidates?: unknown[]
  selected_condition_ids?: string[]
  skipped_reasons?: Record<string, string> | Array<{ condition_id?: string; reason?: string }>
  total_budget_usdc?: DecimalStr
  buy_budget_usdc?: DecimalStr
  allocator?: string | null
}

export type MarketSettledPayload = {
  winning_token_id?: string | null
  winning_outcome?: string | null
  settled_at?: Iso | null
  source?: string | null
  payout_per_share?: DecimalStr | null
  fair_value_at_close?: DecimalStr | null
}

export type SportsLiveStateRecordedPayload = {
  source?: string
  observed_at?: Iso
  signal_allowed?: boolean | null
  signal_reason?: string | null
  phase?: string | null
  live_state_payload?: Record<string, unknown>
  match_payload?: Record<string, unknown>
}

export type BalanceUpdatedPayload = {
  balance_usdc?: DecimalStr | null
  allowance_usdc?: DecimalStr | null
  user_ws_connected?: boolean | null
  allow_new_entries?: boolean | null
  market_pauses?: Record<string, unknown> | unknown[]
  last_reconcile_at?: Iso | null
}

/**
 * 高频 event_type → payload schema 映射。
 * 与后端 _FIELD_PROJECTIONS 字段保持一致；未列出的 event_type 由 base 类型兜底。
 */
export type AuditEventPayloadByType = {
  parameter_override_applied: ParameterOverrideAppliedPayload
  trading_paused: TradingToggleEventPayload
  trading_resumed: TradingToggleEventPayload
  order_cancel_requested: OrderActionEventPayload
  order_cancelled: OrderActionEventPayload
  replace_order_submitted: OrderActionEventPayload
  risk_rejection_recorded: RiskRejectionRecordedPayload
  allocation_decision_recorded: AllocationDecisionRecordedPayload
  market_settled: MarketSettledPayload
  sports_live_state_recorded: SportsLiveStateRecordedPayload
  balance_updated: BalanceUpdatedPayload
}

export type KnownAuditEventType = keyof AuditEventPayloadByType

/** Narrow 后的 AuditEventRow：payload 类型按 event_title 精确化。
 *
 * 用 distributive conditional type 让 union 在 mapped type 上展开——这样
 * `if (e.event_title === 'parameter_override_applied') { e.payload }` 时 TS
 * 能把 payload narrow 到对应 schema，而不是看到 union of all payloads。 */
export type KnownAuditEvent<T extends KnownAuditEventType = KnownAuditEventType> = T extends KnownAuditEventType
  ? Omit<AuditEventRow, 'event_title' | 'payload'> & {
      event_title: T
      payload: AuditEventPayloadByType[T]
    }
  : never

/** Narrow 后的 OutboxPendingRow：payload 类型按 event_type 精确化。同 distributive 套路。 */
export type KnownOutboxEvent<T extends KnownAuditEventType = KnownAuditEventType> = T extends KnownAuditEventType
  ? Omit<OutboxPendingRow, 'event_type' | 'payload'> & {
      event_type: T
      payload: AuditEventPayloadByType[T]
    }
  : never

const KNOWN_AUDIT_EVENT_TITLES: ReadonlySet<KnownAuditEventType> = new Set<KnownAuditEventType>([
  'parameter_override_applied',
  'trading_paused',
  'trading_resumed',
  'order_cancel_requested',
  'order_cancelled',
  'replace_order_submitted',
  'risk_rejection_recorded',
  'allocation_decision_recorded',
  'market_settled',
  'sports_live_state_recorded',
  'balance_updated',
])

function isKnownAuditEventType(value: string | null | undefined): value is KnownAuditEventType {
  return typeof value === 'string' && KNOWN_AUDIT_EVENT_TITLES.has(value as KnownAuditEventType)
}

/**
 * Narrow AuditEventRow → KnownAuditEvent；event_title 不在白名单或 payload 缺失时返回 null。
 * 不做运行时字段验证（task 约束：不引入 zod / 任何运行时验证）；
 * callsite 仅依赖后端 _FIELD_PROJECTIONS 已经过的字段白名单。
 */
export function narrowAuditEvent(event: AuditEventRow): KnownAuditEvent | null {
  if (!isKnownAuditEventType(event.event_title) || event.payload == null) {
    return null
  }
  return event as KnownAuditEvent
}

/** OutboxPendingRow / OutboxFailureRow 同源；narrow 到 KnownOutboxEvent。 */
export function narrowOutboxEvent(event: OutboxPendingRow): KnownOutboxEvent | null {
  if (!isKnownAuditEventType(event.event_type) || event.payload == null) {
    return null
  }
  return event as KnownOutboxEvent
}

// ---------- Portfolio ----------

// 字段与后端 portfolio_aggregator.snapshot() 完全对齐
// (delete legacy aliases equity_usdc / cash_usdc / recent_allocations / positions/ updated_at)
export type PortfolioSnapshot = {
  balance_usdc?: DecimalStr | null
  allowance_usdc?: DecimalStr | null
  available_usdc?: DecimalStr | null
  net_value_usdc?: DecimalStr | null
  notional_usdc?: DecimalStr | null
  cost_usdc?: DecimalStr | null
  cash_pnl_usdc?: DecimalStr | null
  realized_pnl_usdc?: DecimalStr | null
  position_count?: number
  open_position_count?: number
  open_order_count?: number
  fill_count?: number
  pause_count?: number
  markets_tracked?: number
  user_ws_connected?: boolean
  allow_new_entries?: boolean
  last_reconcile_at?: Iso | null
}

export type EquityPoint = { ts: Iso; net_usdc: DecimalStr }

export type EquityCurve = {
  window_ms: number
  interval_ms: number
  points: EquityPoint[]
}

export type PnlBreakdownRow = {
  group_key: string
  position_count: number
  realized_pnl_usdc: DecimalStr
  cash_pnl_usdc: DecimalStr
  current_value_usdc: DecimalStr
  cost_usdc: DecimalStr
}

export type PnlBreakdownGroupBy =
  | 'market_slug'
  | 'condition_id'
  | 'category'
  | 'outcome'
  | 'redeemable_status'

export type PnlBreakdown = {
  group_by: PnlBreakdownGroupBy
  rows: PnlBreakdownRow[]
  totals: {
    position_count: number
    realized_pnl_usdc: DecimalStr
    cash_pnl_usdc: DecimalStr
    current_value_usdc: DecimalStr
    cost_usdc: DecimalStr
  }
}

// ---------- 候选 / Live state ----------

// 字段与后端 candidates_aggregator._serialize 完全对齐.
// intent.metadata.kelly 含 Kelly 内核(prob_p/edge_net/f_star/fee/capped_by/
// buy_budget),是操盘盯盘行内观测的核心信息——不再 [key:string]:unknown 吞掉.

export type KellyInternals = {
  prob_p?: DecimalStr | null
  prob_confidence?: DecimalStr | null
  price_c?: DecimalStr | null
  edge_gross?: DecimalStr | null
  edge_net?: DecimalStr | null
  fee_per_share_usdc?: DecimalStr | null
  f_star?: DecimalStr | null
  effective_kelly_fraction?: DecimalStr | null
  effective_min_stake_usdc?: DecimalStr | null
  capped_by?: string | null
  is_round_up_overbet?: boolean
  buy_budget_usdc?: DecimalStr | null
  target_budget_usdc?: DecimalStr | null
  current_exposure_usdc?: DecimalStr | null
}

export type CandidateIntent = {
  trace_id?: string | null
  condition_id?: string | null
  token_id?: string | null
  market_slug?: string | null
  side?: 'BUY' | 'SELL' | null
  order_type?: string | null
  price?: DecimalStr | null
  amount_usdc?: DecimalStr | null
  size_shares?: DecimalStr | null
  order_id?: string | null
  reason?: string | null
  allow_open_exit_overlap?: boolean
  metadata?: {
    kelly?: KellyInternals
    signal_at?: Iso
    [key: string]: unknown
  }
}

export type CandidateAllocation = {
  buy_budget_usdc?: DecimalStr | null
  target_budget_usdc?: DecimalStr | null
  current_exposure_usdc?: DecimalStr | null
  prob_p?: DecimalStr | null
  edge_net?: DecimalStr | null
  kelly_f_star?: DecimalStr | null
  [key: string]: unknown
}

export type Candidate = {
  candidate_id: string
  trace_id?: string | null
  condition_id: string
  token_id?: string | null
  outcome?: string | null
  market_slug?: string | null
  event_slug?: string | null
  event_title?: string | null
  market_type?: string | null
  side?: string | null
  line?: DecimalStr | null
  best_ask?: DecimalStr | null
  // 状态
  ready_to_trade?: boolean
  accepted?: boolean
  confirmable?: boolean
  manual_confirmed?: boolean
  confirmed_by?: string | null
  confirm_reason?: string | null
  action?: string | null
  decision_action?: string | null
  decision_kind?: string | null
  execution_permission?: string | null
  reason?: string | null
  label?: string | null
  // live state
  signal_allowed?: boolean | null
  live_state_age_ms?: number | null
  live_state_source?: string | null
  // 量化内核
  allocation?: CandidateAllocation | null
  intent?: CandidateIntent | null
  extras?: Record<string, unknown>
  payload?: Record<string, unknown>
  [key: string]: unknown
}

export type CandidatesPage = Page<Candidate>

export type LiveStateRow = {
  condition_id?: string | null
  market_slug?: string | null
  event_slug?: string | null
  source?: string | null
  signal_allowed?: boolean | null
  signal_reason?: string | null
  live_state_signal_allowed?: boolean | null
  live_state_signal_reason?: string | null
  live_state_phase?: string | null
  live_state_payload?: Record<string, unknown> | null
  payload?: Record<string, unknown>
  updated_at?: Iso | null
  [key: string]: unknown
}

export type LiveStatesPage = Page<LiveStateRow>

export type LiveSourceGapRow = {
  condition_id?: string | null
  market_slug?: string | null
  event_slug?: string | null
  prefix?: string | null
  expected_start_at?: Iso | null
  last_live_state_at?: Iso | null
  urgency?: string
  reason?: string
  [key: string]: unknown
}

export type LiveSourceGapsPage = Page<LiveSourceGapRow>

// ---------- Analytics ----------

export type FunnelStage = { name: string; count: number }

export type FunnelSnapshot = {
  window_ms: number
  end_ms?: number | null
  stages: FunnelStage[]
  [key: string]: unknown
}

export type RejectionBucket = {
  key: string
  count: number
  pct: number
}

export type RejectionsSnapshot = {
  window_ms: number
  generated_at: Iso
  total: number
  top: RejectionBucket[]
  filters: { league: string | null; market_type: string | null }
}

export type ExecutionQualitySnapshot = {
  window_ms: number
  generated_at: Iso
  submit_latency_ms: { p50: number | null; p95: number | null }
  fill_latency_ms: { p50: number | null; p95: number | null }
  slippage_bps: { mean: number | null; p95: number | null }
  sample_size: number
  filters: { league: string | null; market_type: string | null }
}

export type EdgeRealizationItem = {
  record_id: string
  trace_id: string
  condition_id: string
  token_id?: string | null
  market_slug?: string | null
  created_at?: Iso | null
  fair_value?: DecimalStr | null
  entry_price?: DecimalStr | null
  predicted_edge_bps?: DecimalStr | null
  cost_usdc?: DecimalStr | null
  cash_pnl_usdc?: DecimalStr | null
  realized_pnl_usdc?: DecimalStr | null
  actual_return_bps?: DecimalStr | null
  position_status: 'open' | 'redeemable' | 'settled_zero' | 'missing' | string
}

export type EdgeRealizationBucket = {
  bucket: string
  count: number
  with_return_count: number
  mean_actual_return_bps?: DecimalStr | null
  median_actual_return_bps?: DecimalStr | null
  win_rate?: DecimalStr | null
}

export type EdgeRealizationSnapshot = {
  limit: number
  condition_id?: string | null
  items: EdgeRealizationItem[]
  buckets: EdgeRealizationBucket[]
  [key: string]: unknown
}

// ---------- Outbox / 运维 ----------

export type OutboxPendingRow = {
  event_id: string
  event_type: string
  trace_id?: string | null
  condition_id?: string | null
  token_id?: string | null
  retry_count?: number
  created_at: Iso
  updated_at?: Iso
  payload?: Record<string, unknown>
  [key: string]: unknown
}

export type OutboxPendingPage = Page<OutboxPendingRow>

export type OutboxFailureRow = OutboxPendingRow & {
  last_error?: string | null
  last_retry_at?: Iso | null
  retry_count: number
}

export type OutboxFailuresPage = Page<OutboxFailureRow>

export type ReconcileDiffRow = {
  event_id: string
  trace_id?: string | null
  condition_id?: string | null
  token_id?: string | null
  market_slug?: string | null
  action_type?: string | null
  target_size_shares?: DecimalStr | null
  target_notional_usdc?: DecimalStr | null
  pause_reason?: string | null
  status?: string
  created_at: Iso
  metadata?: Record<string, unknown>
  [key: string]: unknown
}

export type ReconcileDiffsPage = Page<ReconcileDiffRow>

// ---------- Trade Replays ----------

export type TradeReplayRow = {
  replay_id?: string
  trace_id?: string | null
  condition_id?: string | null
  token_id?: string | null
  market_slug?: string | null
  decision_at?: Iso | null
  order_at?: Iso | null
  fill_at?: Iso | null
  decision_summary?: Record<string, unknown>
  order_summary?: Record<string, unknown>
  fill_summary?: Record<string, unknown>
  [key: string]: unknown
}

export type TradeReplaysPage = Page<TradeReplayRow>

// ---------- Trade Timeline (P0 #1) ----------

export type TimelineEventBase = {
  kind: string
  timestamp: Iso | null
  trace_id?: string | null
  token_id?: string | null
}

export type TimelineDecisionEvent = TimelineEventBase & {
  kind: 'decision'
  hook_name?: string | null
  accepted: boolean
  reason?: string | null
  record_id: string
  decision_input?: Record<string, unknown>
  decision_output?: Record<string, unknown>
}

export type TimelineOrderEvent = TimelineEventBase & {
  kind: 'order'
  order_id?: string
  trade_id?: string | null
  side?: string
  order_type?: string
  status?: string
  price?: DecimalStr | null
  filled_shares?: DecimalStr | null
  remaining_shares?: DecimalStr | null
  notional_usdc?: DecimalStr | null
  reason?: string | null
  full?: Record<string, unknown>
}

export type TimelineFillEvent = TimelineEventBase & {
  kind: 'fill'
  order_id?: string | null
  trade_id?: string | null
  side?: string | null
  price?: DecimalStr | null
  size?: DecimalStr | null
  notional_usdc?: DecimalStr | null
  event_type?: string | null
  full?: Record<string, unknown>
}

export type TimelineAuditEvent = TimelineEventBase & {
  kind: 'audit'
  event_title?: string
  status?: string | null
  reason?: string | null
  /** 后端 trade timeline 把整条 audit_event 透传为 full；用 AuditEventRow 让 callsite
   *  能直接走 narrowAuditEvent narrow 到具体 event_title payload schema。 */
  full?: AuditEventRow
}

export type TimelineOutboxEvent = TimelineEventBase & {
  kind: 'outbox'
  event_type?: string
  reason?: string | null
  retry_count?: number
  last_error?: string | null
  full?: Record<string, unknown>
}

export type TimelineEvent =
  | TimelineDecisionEvent
  | TimelineOrderEvent
  | TimelineFillEvent
  | TimelineAuditEvent
  | TimelineOutboxEvent

export type TradeTimeline = {
  condition_id: string
  token_id?: string | null
  event_count: number
  truncated: boolean
  events: TimelineEvent[]
  current_position?: PositionRow | null
}

// ---------- Operations 写操作响应 ----------

export type WriteOperationResult = {
  status: 'ok' | 'failed' | 'pending' | string
  trace_id?: string | null
  reason?: string | null
  audit_event_id?: string | null
  [key: string]: unknown
}

export type VirtualPaperTradeResult = {
  status: string
  reason?: string | null
  data_source?: string
  execution?: string
  decision_output?: Record<string, unknown>
  [key: string]: unknown
}

// ---------- 实时调参 (/parameters) ----------
// 后端契约：白名单 registry，scope+key 唯一标识。重启即丢；写入自动落
// PARAMETER_OVERRIDE_APPLIED audit_event。

export type ParameterScope = 'settings' | 'strategy' | string

export type ParameterOverride = {
  scope: string
  key: string
  description: string
  /** 后端会把 Decimal 转字符串、嵌套结构化值递归 stringify。 */
  value: unknown
  operator: string
  applied_at: Iso
  expires_at: Iso | null
  reason: string | null
}

export type ParameterRegistryEntry = {
  scope: string
  key: string
  description: string
  /** 启动时从 Settings / 策略配置读取的基准默认值；无则 null。 */
  default_value: string | null
  override: ParameterOverride | null
}

export type ParametersRegistry = {
  parameters: ParameterRegistryEntry[]
  active_override_count: number
}

export type ParameterOverridesList = {
  overrides: ParameterOverride[]
}

export type ParameterSetRequest = {
  value: unknown
  operator: string
  reason?: string | null
  expires_at?: string | null
  trace_id?: string | null
}

export type ParameterClearRequest = {
  operator: string
  reason?: string | null
  trace_id?: string | null
}

export type ParameterClearResult = {
  scope: string
  key: string
  cleared: true
  operator: string
  applied_at: Iso
  previous_value: unknown
}

// ---------- Risk Rejections (P0 #5) ----------

export type RiskCheck = {
  name: string
  field?: string | null
  value?: unknown
  passed?: boolean
  suggested_action?: string | null
}

export type RiskRejectionEvent = AuditEventRow & {
  payload?: {
    checks?: RiskCheck[]
    [key: string]: unknown
  }
}

export type RiskRejectionsPage = Page<RiskRejectionEvent>

export type RiskRejectionAggregate = {
  total_rejections: number
  by_check_name: Array<{ check_name: string; count: number }>
  by_failed_field: Array<{ field: string; count: number }>
}

// ---------- Calibration (P0 #4) ----------

export type CalibrationBucket = {
  lower: DecimalStr
  upper: DecimalStr
  prediction_count: number
  outcome_count: number
  mean_prediction: DecimalStr | null
  empirical_rate: string | null
}

export type CalibrationSnapshot = {
  bucket_size: DecimalStr
  buckets: CalibrationBucket[]
  brier_score: string | null
  log_loss: string | null
  total_samples: number
  with_outcome_count: number
}

// ---------- Sports Live Events 历史 (P0 #6) ----------
// payload 形态由 sports_live_state_worker 决定：
//   { source, observed_at, signal_allowed, signal_reason, phase, match_payload }
// 比赛细节（score / clock / 等）在 match_payload 内，strategy-defined。
// POST /candidates/live-states 手工录入的事件 payload 是 caller 自定义的，可能形态不同。

export type SportsLiveEvent = AuditEventRow & {
  payload?: {
    source?: string
    observed_at?: Iso
    signal_allowed?: boolean | null
    signal_reason?: string | null
    phase?: string | null
    match_payload?: Record<string, unknown>
    [key: string]: unknown
  }
}

export type SportsLiveEventsPage = Page<SportsLiveEvent>

// ---------- Missed Opportunities (P1 #13) ----------

export type MissedOpportunityStatus =
  | 'would_have_won'
  | 'would_have_lost'
  | 'unsettled'
  | 'unscorable'
  | string

export type MissedOpportunityItem = {
  record_id: string
  trace_id: string
  condition_id: string
  token_id: string | null
  market_slug: string | null
  created_at: Iso | null
  reason: string | null
  entry_price: DecimalStr | null
  fair_value: DecimalStr | null
  winning_token_id: string | null
  settled_at: Iso | null
  hypothetical_pnl_usdc: DecimalStr | null
  status: MissedOpportunityStatus
}

export type MissedOpportunityReasonBucket = {
  reason: string
  decision_count: number
  settled_count: number
  would_have_won_count: number
  would_have_lost_count: number
  hypothetical_pnl_usdc: DecimalStr
}

export type MissedOpportunitiesSnapshot = {
  items: MissedOpportunityItem[]
  by_reason: MissedOpportunityReasonBucket[]
  totals: {
    decision_count: number
    settled_count: number
    would_have_won_count: number
    would_have_lost_count: number
    hypothetical_pnl_usdc: DecimalStr
  }
  per_decision_usdc: DecimalStr
}

// ---------- Market Settlement (P1 #10) ----------

export type MarketSettlement = {
  condition_id: string
  winning_token_id: string | null
  winning_outcome: string | null
  settled_at: Iso | null
  source: string | null
  operator: string | null
  /** 最后一次 accepted 决策的 fair_value（若有）。 */
  last_accepted_fair_value: DecimalStr | null
  /** outcome(0/1) - fair_value；正 = 低估了赢家，负 = 高估。 */
  fair_value_deviation: DecimalStr | null
  raw_payload?: Record<string, unknown>
  [key: string]: unknown
}

export type MarketSettlementsPage = Page<AuditEventRow>

export type SettleMarketRequest = {
  condition_id: string
  winning_token_id: string
  winning_outcome?: string | null
  source?: string
  operator: string
}

// ---------- Allocation Decisions (P1 #11) ----------

export type AllocationDecisionEvent = AuditEventRow & {
  payload?: {
    candidates?: unknown[]
    selected_condition_ids?: string[]
    skipped_reasons?: Record<string, string> | Array<{ condition_id?: string; reason?: string }>
    budget?: DecimalStr | Record<string, unknown>
    [key: string]: unknown
  }
}

export type AllocationDecisionsPage = Page<AllocationDecisionEvent>

// ---------- Portfolio Risk Metrics (P2 #15) ----------

export type RiskMetricsPayload = {
  sample_count: number
  first_recorded_at: Iso | null
  last_recorded_at: Iso | null
  start_net_value_usdc: DecimalStr | null
  end_net_value_usdc: DecimalStr | null
  /** 后端返回 6 位小数字符串（float）。 */
  total_return: string | null
  /** 最大回撤；DecimalStr，单位与 net_value 一致（注：后端命名 max_drawdown_pct，但实际是 net_value 绝对值差）。 */
  max_drawdown_pct: DecimalStr
  peak_at: Iso | null
  trough_at: Iso | null
  time_underwater_seconds: number
  time_underwater_ratio: string | null
  mean_return_per_period: string | null
  stddev_return_per_period: string | null
  /** sharpe_like = mean / stddev × sqrt(annualization_factor)（若提供）。 */
  sharpe_like: string | null
  annualization_factor: number | null
}

export type PortfolioRiskMetrics = {
  window_ms: number
  interval_ms: number
  metrics: RiskMetricsPayload
}

// ---------- Operator Interventions Aggregate (plan §13.17) ----------

export type OperatorAggregateEvent = {
  event_id: string
  event_title: string
  operator: string
  reason: string | null
  condition_id: string | null
  token_id: string | null
  created_at: Iso
}

export type OperatorInterventionsAggregate = {
  /** 请求时指定的 operator；null 表示全 operator 总分布。 */
  operator: string | null
  total_events: number
  by_operator: Array<{ operator: string; count: number }>
  by_event_title: Array<{ event_title: string; count: number }>
  /** 指定 operator 时返回最近 200 条；总览时也填充近期样本。 */
  events: OperatorAggregateEvent[]
}

// ---------- Parameter Sweep (P2 #18) ----------
// 后端 _SUPPORTED_PARAMETERS 白名单——前端按这个清单做预设输入框。
// strategy parameter_store 的 (scope, key) 映射全在 strategy scope。

export type SweepParameterKey =
  | 'outright_min_edge_bps'
  | 'outright_max_entry_price'
  | 'outright_min_orderbook_depth_usdc'
  | 'entry_no_price_max'

export type SweepCandidateResult = {
  /** 该组合实际生效的参数（后端把 Decimal stringify）。 */
  parameters: Record<string, string | number>
  would_have_entered_count: number
  settled_count: number
  pending_unsettled_count: number
  win_count: number
  loss_count: number
  hypothetical_pnl_usdc: DecimalStr
  sum_win_pnl_usdc: DecimalStr
  sum_loss_pnl_usdc: DecimalStr
  /** float 6 位小数；settled_count=0 时为 null。 */
  win_rate: string | null
  mean_pnl_per_entered_usdc: DecimalStr | null
}

export type SweepParamSpec = {
  key: SweepParameterKey
  type: 'int' | 'decimal'
  label: string
  input_hint: string
  example: string
  scope: 'strategy'
  minimum?: string | null
  minimum_exclusive?: string | null
  maximum?: string | null
  maximum_exclusive?: string | null
}

export type ParameterSweepRequest = {
  /** 候选键值；笛卡尔积上限 1000。 */
  candidates: Partial<Record<SweepParameterKey, Array<number | string>>>
  per_decision_usdc?: number
  since?: number
  until?: number
  decision_limit?: number
  settlement_limit?: number
}

export type ParameterSweepResponse = {
  candidate_count: number
  decision_sample_count: number
  scorable_decision_count: number
  unscorable_decision_count: number
  /**
   * 非零时警告：评估时 best_ask 缺失，回退用 entry_price_cap，
   * hypothetical PnL 系统性偏高。
   */
  entry_price_cap_fallback_count: number
  per_decision_usdc: DecimalStr
  supported_parameter_keys: string[]
  /** 按 hypothetical_pnl_usdc 降序。 */
  results: SweepCandidateResult[]
  best_by_pnl: SweepCandidateResult | null
  best_by_win_rate: SweepCandidateResult | null
}

// ---------- 新增：操盘缺口补全类型 ----------

export type PortfolioExposureItem = {
  condition_id: string
  token_id: string
  market_slug: string | null
  shares: DecimalStr
  cost_usdc: DecimalStr
  notional_usdc: DecimalStr
  avg_price: DecimalStr | null
  cur_price: DecimalStr | null
  cash_pnl: DecimalStr | null
  percent_pnl: DecimalStr | null
  realized_pnl: DecimalStr | null
  open_buy_reserved_usdc: DecimalStr
  /** 仅当人工 click 触发的暂停（MarketPauseSource.MANUAL）才为 true。
   * 后台 reconcile/risk/strategy 自动 pause 不在此暴露——它们是市场状态，
   * 与仓位生命周期无关。 */
  paused: boolean
  redeemable: boolean | null
  settled_zero_value: boolean
}

export type PortfolioExposure = {
  items: PortfolioExposureItem[]
  position_count: number
  total_notional_usdc: DecimalStr
  total_cost_usdc: DecimalStr
  total_cash_pnl: DecimalStr
  total_open_buy_reserved_usdc: DecimalStr
  available_usdc: DecimalStr
  balance_usdc: DecimalStr
  equity_usdc: DecimalStr
}

export type DepthTier = {
  tick: string
  price: DecimalStr
  size: DecimalStr
  cumulative_size: DecimalStr
  cumulative_usdc: DecimalStr
}

export type MarketLiquidity = {
  token_id: string
  condition_id: string | null
  market_slug: string | null
  snapshot_age_ms: number
  best_bid: DecimalStr | null
  best_ask: DecimalStr | null
  best_bid_size: DecimalStr | null
  best_ask_size: DecimalStr | null
  spread: DecimalStr | null
  effective_spread_bps: DecimalStr | null
  vwap_mid: DecimalStr | null
  vwap_bid: DecimalStr | null
  vwap_ask: DecimalStr | null
  bid_depth: DepthTier[]
  ask_depth: DepthTier[]
  total_bid_size: DecimalStr
  total_ask_size: DecimalStr
}

export type MarketImpact = {
  token_id: string
  condition_id: string | null
  market_slug: string | null
  snapshot_age_ms: number
  requested_usdc: DecimalStr
  fillable_usdc: DecimalStr
  unfillable_usdc: DecimalStr
  estimated_shares: DecimalStr | null
  estimated_avg_price: DecimalStr | null
  best_ask: DecimalStr | null
  price_impact_bps: DecimalStr | null
  fully_fillable: boolean
}

export type QueueLaneDepth = {
  depth: number
  capacity: number
  retained: number
  utilization_pct: DecimalStr | null
}

export type OutboxQueueDepth = {
  available: boolean
  trading?: QueueLaneDepth
  maintenance?: QueueLaneDepth
  persistence?: QueueLaneDepth
  low_priority_paused?: boolean
}

export type BulkCancelResultItem = {
  order_id: string
  trace_id: string
  status: 'ok' | 'failed'
  reason: string
}

export type BulkCancelResult = {
  submitted: number
  succeeded: number
  failed: number
  operator: string
  parent_trace_id: string | null
  results: BulkCancelResultItem[]
}

export type DataFreshnessItem = {
  condition_id: string | null
  market_slug: string | null
  event_slug: string | null
  source: string | null
  has_live_state: boolean
  signal_allowed: boolean | null
  staleness_ms: number | null
  updated_at: string | null
}

export type DataFreshnessSnapshot = {
  available: boolean
  item_count: number
  items: DataFreshnessItem[]
}
