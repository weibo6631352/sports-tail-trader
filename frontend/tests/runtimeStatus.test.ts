import type { PortfolioSnapshot, ReadyPayload, RuntimeStatus, WorkersPayload } from '../src/core/api/types'
import { formatAllowance } from '../src/shared/utils/format'
import {
  formatConfirmationStatusLabel,
  formatOrderSideLabel,
  formatOrderStatusLabel,
  formatPhaseLabel,
  formatTradingStatusLabel,
  formatWorkerStateLabel,
  isSellOrderSide,
} from '../src/shared/utils/labels'

const assert = (condition: unknown, message: string): void => {
  if (!condition) {
    throw new Error(message)
  }
}

const runtimeStatus = {
  phase: 'trading_enabled',
  ready_to_trade: true,
  automatic_trading_enabled: true,
  user_ws_connected: true,
  allow_new_entries: true,
  last_reconcile_at: '2026-04-23T01:26:09.531218+00:00',
  blocking_reasons: [],
  warnings: [],
} satisfies RuntimeStatus

const readyPayload = {
  ready_to_trade: true,
  phase: 'trading_enabled',
  blocking_issues: [],
  warnings: [],
  runtime: runtimeStatus,
} satisfies ReadyPayload

const workersPayload = {
  phase: 'trading_enabled',
  automatic_trading_enabled: true,
  queue_depths: {},
  scheduler: null,
  workers: [],
} satisfies WorkersPayload

const portfolio = {
  balance_usdc: '5',
  allowance_usdc: '5',
  available_usdc: '5',
  position_count: 0,
  open_order_count: 0,
  fill_count: 0,
  pause_count: 0,
  last_reconcile_at: null,
  user_ws_connected: true,
  allow_new_entries: true,
  markets_tracked: 0,
  recent_allocations: [],
} satisfies PortfolioSnapshot

assert(readyPayload.runtime.allow_new_entries, 'ready runtime should expose allow_new_entries')
assert(workersPayload.automatic_trading_enabled, 'workers payload should expose trading gate state')
assert(portfolio.allow_new_entries, 'portfolio should expose allow_new_entries')
assert(formatPhaseLabel('trading_enabled') === '交易已启用', 'trading_enabled should render as Chinese')
assert(formatPhaseLabel('config_loading') === '读取配置中', 'config_loading should render as Chinese')
assert(formatPhaseLabel('infra_ready') === '基础设施已就绪', 'infra_ready should render as Chinese')
assert(formatPhaseLabel('reconciling') === '启动对账中', 'reconciling should render as Chinese')
assert(formatPhaseLabel('stopping') === '停止中', 'stopping should render as Chinese')
assert(formatPhaseLabel('stopped') === '已停止', 'stopped should render as Chinese')
assert(formatWorkerStateLabel('degraded') === '降级运行', 'degraded worker state should render as Chinese')
assert(formatTradingStatusLabel('candidate') === '候选中', 'candidate trading status should render as Chinese')
assert(formatOrderSideLabel('BUY') === '买入', 'uppercase BUY should render as Chinese')
assert(formatOrderSideLabel('SELL') === '卖出', 'uppercase SELL should render as Chinese')
assert(isSellOrderSide('SELL'), 'uppercase SELL should be recognized as sell side')
assert(!isSellOrderSide('BUY'), 'uppercase BUY should not be recognized as sell side')
assert(formatOrderStatusLabel('created') === '已创建', 'created order status should render as Chinese')
assert(formatOrderStatusLabel('signed') === '已签名', 'signed order status should render as Chinese')
assert(formatOrderStatusLabel('submitted') === '已提交', 'submitted order status should render as Chinese')
assert(formatOrderStatusLabel('cancel_requested') === '撤单中', 'cancel_requested order status should render as Chinese')
assert(formatOrderStatusLabel('no_fill') === '未成交', 'no_fill order status should render as Chinese')
assert(formatOrderStatusLabel('full_fill') === '已成交', 'full_fill order status should render as Chinese')
assert(formatConfirmationStatusLabel('matched') === '已成交', 'matched confirmation status should render as Chinese')
assert(formatConfirmationStatusLabel('full_fill') === '已成交', 'full_fill confirmation status should render as Chinese')
assert(
  formatConfirmationStatusLabel('partial_fill') === '部分成交',
  'partial_fill confirmation status should render as Chinese',
)
assert(
  formatConfirmationStatusLabel('unknown_timeout') === '超时未知',
  'unknown_timeout confirmation status should render as Chinese',
)
assert(formatAllowance('5') === '5', 'finite allowance should display as a number')
assert(
  formatAllowance('115792089237316195423570985008687907853269984665640564039457584007.913129639935') ===
    '无限授权',
  'unlimited allowance should display as unlimited',
)
