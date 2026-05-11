// 前端 metadata overlay：后端 /parameters registry 是真相来源（白名单），
// 前端按 (scope, key) 叠加 UI 友好的字段——风险等级、分组、输入提示、placeholder。
// 不在 overlay 中的字段仍可显示，按默认 medium risk 渲染。

export type ParamRisk = 'low' | 'medium' | 'high'
export type ParamInputHint = 'decimal' | 'integer' | 'price_0_to_1' | 'usdc' | 'bps' | 'text'

export type ParamMetadata = {
  risk: ParamRisk
  group: string
  inputHint: ParamInputHint
  hint?: string
}

const META: Record<string, ParamMetadata> = {
  // settings 段 —— 全部高风险，直接影响资金敞口与挂单上限。
  'settings.portfolio_budget_usdc': {
    risk: 'high',
    group: '组合预算',
    inputHint: 'usdc',
    hint: '组合总预算上限；缩小会立即影响后续 allocation。',
  },
  'settings.max_order_usdc': {
    risk: 'high',
    group: '订单上限',
    inputHint: 'usdc',
    hint: '单笔订单最大投入。',
  },
  'settings.max_market_usdc': {
    risk: 'high',
    group: '组合预算',
    inputHint: 'usdc',
    hint: '同一市场累计最大暴露。',
  },
  'settings.max_total_usdc': {
    risk: 'high',
    group: '组合预算',
    inputHint: 'usdc',
    hint: '组合累计暴露上限。',
  },
  'settings.max_open_orders': {
    risk: 'medium',
    group: '订单上限',
    inputHint: 'integer',
    hint: '账户级 open order 数量上限。',
  },
  'settings.order_retry_limit': {
    risk: 'low',
    group: '订单上限',
    inputHint: 'integer',
  },
  // strategy 段 —— 风控/入场价直接影响成单。
  'strategy.tail_outright_min_edge_bps': {
    risk: 'high',
    group: 'Outright 定价',
    inputHint: 'bps',
    hint: '最小入场 edge（bps）；缩小 = 入场门槛降低 = 单更多但胜率可能下降。',
  },
  'strategy.tail_outright_max_entry_price': {
    risk: 'high',
    group: 'Outright 定价',
    inputHint: 'price_0_to_1',
    hint: '最大入场价（0–1）；扩大会接到更贵的标的。',
  },
  'strategy.tail_outright_min_orderbook_depth_usdc': {
    risk: 'medium',
    group: 'Outright 流动性',
    inputHint: 'usdc',
  },
  'strategy.tail_outright_exit_edge_target': {
    risk: 'medium',
    group: 'Outright 退出',
    inputHint: 'decimal',
    hint: '目标 edge（小数；0.03 = 3%）。',
  },
  'strategy.tail_outright_min_profit_per_share': {
    risk: 'medium',
    group: 'Outright 退出',
    inputHint: 'decimal',
  },
  'strategy.entry_no_price_max': {
    risk: 'high',
    group: 'No-side 入场',
    inputHint: 'price_0_to_1',
    hint: 'No-side 允许的最大价格——安全阈值，避免高位接盘。',
  },
  'strategy.tail_moneyline_max_entry_price': {
    risk: 'high',
    group: 'Moneyline tail',
    inputHint: 'price_0_to_1',
  },
  'strategy.tail_spreads_max_entry_price': {
    risk: 'high',
    group: 'Spread tail',
    inputHint: 'price_0_to_1',
  },
  'strategy.tail_min_liquidity_usdc': {
    risk: 'medium',
    group: '通用 tail 流动性',
    inputHint: 'usdc',
  },
  'strategy.tail_outright_budget_usdc': {
    risk: 'high',
    group: 'Outright 预算',
    inputHint: 'usdc',
    hint: 'Outright 家族总预算（USDC）；默认 0 = 全部 outright 拒绝（仅审计）。启用自动交易需 ≥ tail_outright_max_per_market_usdc。',
  },
  'strategy.tail_stale_no_live_state_seconds': {
    risk: 'medium',
    group: '直播源诊断',
    inputHint: 'integer',
    hint: '赛事开始后无直播状态超此秒数 → 主动 pause stale market。默认 86400(24h)；0 = 关闭。',
  },
}

export function getParamMetadata(scope: string, key: string): ParamMetadata {
  return (
    META[`${scope}.${key}`] ?? {
      risk: 'medium',
      group: scope === 'settings' ? '其他设置' : '其他策略参数',
      inputHint: 'text',
    }
  )
}
