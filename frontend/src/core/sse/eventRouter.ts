import type { QueryClient } from '@tanstack/react-query'
import { qkRoots } from '@core/api/keys'
import type { SseEvent } from './types'

// 事件 → invalidate 的 query key 列表映射。
// 事件名严格对齐后端 polymarket_trader.domain.events.DomainEventType 枚举。
// 一个事件可触发多个查询失效：例如 fill 同时影响订单 / 持仓 / portfolio。
// 不要在此处放猜测的事件名——加新事件前先在后端枚举确认。

export type EventRoute = {
  match: (event: SseEvent) => boolean
  invalidate: readonly (readonly unknown[])[]
}

const eventTypeMatch = (...types: string[]): EventRoute['match'] => {
  const set = new Set(types)
  return (event) => set.has(event.eventType)
}

export const DEFAULT_EVENT_ROUTES: EventRoute[] = [
  // ---- 订单生命周期 ----
  {
    match: eventTypeMatch(
      'order_created',
      'order_signed',
      'order_submitted',
      'order_rejected',
      'order_state_updated',
      'order_cancel_requested',
      'order_cancelled',
      'order_matched',
      'order_no_fill',
      'order_partially_filled',
      'replace_order_submitted',
      'follow_up_order_submitted',
      'unexpected_resting_order_detected',
    ),
    invalidate: [qkRoots.orders, qkRoots.metrics],
  },
  // ---- 成交 ----
  {
    match: eventTypeMatch('fill_recorded', 'trade_mined', 'trade_confirmed'),
    invalidate: [qkRoots.orders, qkRoots.fills, qkRoots.positions, qkRoots.portfolio, qkRoots.metrics],
  },
  // ---- 持仓 / 余额 ----
  {
    match: eventTypeMatch('position_updated', 'balance_updated'),
    invalidate: [qkRoots.positions, qkRoots.portfolio, qkRoots.metrics],
  },
  // ---- 决策 ----
  {
    match: eventTypeMatch('decision_recorded'),
    invalidate: [qkRoots.decisions, qkRoots.candidates, qkRoots.metrics],
  },
  {
    match: eventTypeMatch('entry_signal_triggered'),
    invalidate: [qkRoots.candidates],
  },
  // ---- 风控（结构化拒绝） ----
  {
    match: eventTypeMatch('risk_check_passed', 'risk_check_failed', 'risk_rejection_recorded'),
    invalidate: [qkRoots.analytics, qkRoots.decisions, qkRoots.auditEvents],
  },
  // ---- 市场 universe / 元数据 ----
  {
    match: eventTypeMatch(
      'market_discovered',
      'market_updated',
      'market_filtered_in',
      'market_filtered_out',
      'market_resolved_or_disabled',
    ),
    invalidate: [qkRoots.markets, qkRoots.candidates],
  },
  // ---- 单市场暂停 ----
  {
    match: eventTypeMatch('trading_paused_for_market'),
    invalidate: [qkRoots.markets, qkRoots.ready, qkRoots.auditEvents],
  },
  // ---- 体育实时事件 ----
  {
    match: eventTypeMatch('sports_live_state_recorded'),
    invalidate: [qkRoots.candidates, qkRoots.sports, qkRoots.auditEvents],
  },
  // ---- AllocationPlan 决策过程 ----
  {
    match: eventTypeMatch('allocation_decision_recorded'),
    invalidate: [qkRoots.allocations, qkRoots.auditEvents],
  },
  // ---- 结算（calibration / missed-opportunities / settlements 都依赖） ----
  {
    match: eventTypeMatch('market_settled'),
    invalidate: [qkRoots.markets, qkRoots.analytics, qkRoots.portfolio, qkRoots.auditEvents],
  },
  // ---- 参数热调 ----
  {
    match: eventTypeMatch('parameter_override_applied'),
    invalidate: [qkRoots.parameters, qkRoots.auditEvents],
  },
  // ---- Reconcile ----
  {
    match: eventTypeMatch('reconcile_started', 'reconcile_diff_detected', 'reconcile_applied'),
    invalidate: [
      qkRoots.operations,
      qkRoots.positions,
      qkRoots.orders,
      qkRoots.portfolio,
      qkRoots.outbox,
    ],
  },
  // 注：orderbook / orderbook_snapshot_updated 高频，不做全 root invalidate；
  //     orderbook 数据是按 token_id 拉取，需要时各页自行查询。
  // 注：retry / skipped / error 是泛型噪声事件，不做路由。

  // ---- SSE 通道事件 ----
  {
    match: (event) => event.channel === 'subscription_lag',
    // 数据落后：保守全表对齐——分析师视图保留显式时间窗，自动刷新主要影响盯盘。
    invalidate: [
      qkRoots.orders,
      qkRoots.positions,
      qkRoots.fills,
      qkRoots.portfolio,
      qkRoots.candidates,
      qkRoots.metrics,
      qkRoots.runtime,
      qkRoots.ready,
    ],
  },
]

export function dispatchEvent(
  client: QueryClient,
  event: SseEvent,
  routes: EventRoute[] = DEFAULT_EVENT_ROUTES,
): void {
  // 提取 event payload 里的 condition_id 做精准失效。
  // 若 event 不带 condition_id（账户级 / 全局事件），保守按 root 全失效。
  const conditionId = extractConditionId(event)

  for (const route of routes) {
    if (!route.match(event)) continue
    for (const key of route.invalidate) {
      if (conditionId === null) {
        void client.invalidateQueries({ queryKey: key, refetchType: 'active' })
      } else {
        void client.invalidateQueries({
          queryKey: key,
          refetchType: 'active',
          // 跳过 queryKey 含 condition_id 但与 event 不一致的——避免无关市场的查询白白 refetch。
          // queryKey 不含 condition_id 字段（如聚合 / 总览）→ 仍然失效，因为可能受这条事件影响。
          predicate: (q) => keyMatchesConditionId(q.queryKey, conditionId),
        })
      }
    }
  }
}

function extractConditionId(event: SseEvent): string | null {
  const raw = event.payload['condition_id']
  if (typeof raw === 'string' && raw.length > 0) return raw
  return null
}

function keyMatchesConditionId(queryKey: readonly unknown[], conditionId: string): boolean {
  // 我们的 query key 形态约定：
  //   [root, action, params?]，params 是 plain object，可能含 condition_id 字段；
  //   或 [root, 'timeline', conditionId, params]——conditionId 作裸字符串 segment。
  // 任意 segment 命中即比较；没找到 condition_id 视为"广域"查询，保守失效。
  for (const seg of queryKey) {
    if (seg && typeof seg === 'object' && 'condition_id' in (seg as Record<string, unknown>)) {
      const v = (seg as Record<string, unknown>).condition_id
      if (typeof v === 'string') {
        return v === conditionId
      }
    }
    // Polymarket condition_id 形如 0x{64hex}——长字符串 segment 视为 condition_id。
    if (typeof seg === 'string' && seg.startsWith('0x') && seg.length >= 10) {
      return seg === conditionId
    }
  }
  return true
}
