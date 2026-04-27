const PHASE_LABELS: Record<string, string> = {
  config_loading: '读取配置中',
  infra_ready: '基础设施已就绪',
  starting: '启动中',
  reconciling: '启动对账中',
  workers_started: '后台线程已启动',
  trading_enabled: '交易已启用',
  degraded: '降级运行',
  recovering_snapshot: '恢复快照中',
  paused: '已暂停',
  stopping: '停止中',
  stopped: '已停止',
}

const WORKER_STATE_LABELS: Record<string, string> = {
  running: '运行中',
  paused: '已暂停',
  starting: '启动中',
  stopped: '已停止',
  idle: '空闲',
  degraded: '降级运行',
  failed: '异常',
}

const TRADING_STATUS_LABELS: Record<string, string> = {
  active: '交易中',
  candidate: '候选中',
  paused: '已暂停',
  closed: '已关闭',
  resolved: '已结算',
  rejected: '已拒绝',
  eligible: '可参与',
}

const ORDER_SIDE_LABELS: Record<string, string> = {
  buy: '买入',
  sell: '卖出',
}

const ORDER_STATUS_LABELS: Record<string, string> = {
  ok: '完成',
  success: '完成',
  created: '已创建',
  signed: '已签名',
  submitted: '已提交',
  cancel_requested: '撤单中',
  matched: '已成交',
  full_fill: '已成交',
  partial_fill: '部分成交',
  partially_filled: '部分成交',
  no_fill: '未成交',
  live: '挂单中',
  placed: '已提交',
  failed: '失败',
  rejected: '已拒绝',
  cancelled: '已取消',
}

const CONFIRMATION_STATUS_LABELS: Record<string, string> = {
  confirmed: '已确认',
  pending: '待确认',
  matched: '已成交',
  full_fill: '已成交',
  partial_fill: '部分成交',
  partially_filled: '部分成交',
  no_fill: '未成交',
  live: '挂单中',
  rejected: '已拒绝',
  failed: '失败',
  cancelled: '已取消',
  unknown_timeout: '超时未知',
  unknown: '未知',
}

const ISSUE_FIELD_LABELS: Record<string, string> = {
  wallet_private_key: '钱包私钥',
  portfolio_budget_usdc: '组合预算',
  max_order_usdc: '单笔下单上限',
  max_market_usdc: '单市场上限',
  max_total_usdc: '总仓上限',
  max_open_orders: '最大未完成订单数',
  database: '数据库连接',
  market_ws_connected: '市场行情连接',
  trading_client: '交易客户端',
  outbox_depth: '外发队列',
  startup_reconcile: '启动对账',
  runtime: '运行态',
  user_ws_connected: '用户行情连接',
  allow_new_entries: '允许新买入',
  last_reconcile_at: '最近一次对账',
}

const normalizeLabelKey = (value: string | null | undefined): string | null => {
  if (!value) {
    return null
  }
  const normalized = value.trim().toLowerCase()
  return normalized || null
}

export const formatPhaseLabel = (value: string | null | undefined): string => {
  const normalized = normalizeLabelKey(value)
  if (!normalized) {
    return '—'
  }
  return PHASE_LABELS[normalized] ?? value
}

export const formatWorkerStateLabel = (value: string | null | undefined): string => {
  const normalized = normalizeLabelKey(value)
  if (!normalized) {
    return '未知'
  }
  return WORKER_STATE_LABELS[normalized] ?? value
}

export const formatTradingStatusLabel = (value: string | null | undefined): string => {
  const normalized = normalizeLabelKey(value)
  if (!normalized) {
    return '未知'
  }
  return TRADING_STATUS_LABELS[normalized] ?? value
}

export const isSellOrderSide = (value: string | null | undefined): boolean => normalizeLabelKey(value) === 'sell'

export const formatOrderSideLabel = (value: string | null | undefined): string => {
  const normalized = normalizeLabelKey(value)
  if (!normalized) {
    return '未知'
  }
  return ORDER_SIDE_LABELS[normalized] ?? value
}

export const formatOrderStatusLabel = (value: string | null | undefined): string => {
  const normalized = normalizeLabelKey(value)
  if (!normalized) {
    return '未知'
  }
  return ORDER_STATUS_LABELS[normalized] ?? value
}

export const formatConfirmationStatusLabel = (value: string | null | undefined): string => {
  const normalized = normalizeLabelKey(value)
  if (!normalized) {
    return '未知'
  }
  return CONFIRMATION_STATUS_LABELS[normalized] ?? value
}

export const formatIssueFieldLabel = (value: string | null | undefined): string => {
  const normalized = normalizeLabelKey(value)
  if (!normalized) {
    return '未知字段'
  }
  return ISSUE_FIELD_LABELS[normalized] ?? value
}
