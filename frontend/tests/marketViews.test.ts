import type { MarketView } from '../src/core/api/types'
import {
  getMarketOpenOrderCount,
  getMarketPosition,
  getMarketTokenId,
  getOutcomeTokenView,
  getPrimaryTokenView,
  hasOpenSellOrders,
} from '../src/shared/utils/marketViews'

const assert = (condition: unknown, message: string): void => {
  if (!condition) {
    throw new Error(message)
  }
}

const market = {
  market: {
    condition_id: 'condition',
    market_slug: 'market-slug',
    event_slug: null,
    event_id: null,
    event_title: 'Market title',
    token_ids: ['yes-token', 'no-token'],
    outcomes: [
      { token_id: 'yes-token', outcome: 'Yes' },
      { token_id: 'no-token', outcome: 'No' },
    ],
    icon_url: null,
    end_date: null,
    tick_size: null,
    min_order_size: null,
    neg_risk: false,
    fees: {
      enabled: false,
      maker_base_fee_bps: null,
      taker_base_fee_bps: null,
      fee_rate_bps: null,
      fee_rate_updated_at: null,
    },
    category: null,
    tags: [],
    matched_keywords: [],
    trading_status: 'eligible',
    reject_reason: null,
  },
  tracked: true,
  token_views: [
    {
      token_id: 'yes-token',
      outcome: 'Yes',
      orderbook: null,
      position: null,
      open_orders: [],
      open_order_count: 0,
      best_ask: '0.51',
      best_bid: '0.50',
      spread: '0.01',
      fee_preview: null,
    },
    {
      token_id: 'no-token',
      outcome: 'No',
      orderbook: null,
      position: {
        condition_id: 'condition',
        token_id: 'no-token',
        market_slug: 'market-slug',
        event_slug: 'event-slug',
        shares: '2',
        cost_usdc: null,
        open_buy_shares: null,
        open_sell_shares: null,
        pending_buy_shares: null,
        confirmed_shares: null,
        last_order_id: null,
        last_trade_id: null,
        confirmation_status: null,
        updated_at: null,
      },
      open_orders: [
        {
          trace_id: null,
          condition_id: 'condition',
          token_id: 'no-token',
          market_slug: 'market-slug',
          event_slug: 'event-slug',
          side: 'SELL',
          order_type: 'limit',
          price: '0.99',
          amount_usdc: null,
          size_shares: '1',
          filled_shares: null,
          remaining_shares: null,
          notional_usdc: null,
          order_id: 'order-1',
          trade_id: null,
          status: 'open',
          idempotency_key: null,
          reason: null,
          post_only: false,
          created_at: null,
          updated_at: null,
        },
      ],
      open_order_count: 1,
      best_ask: '0.49',
      best_bid: '0.48',
      spread: '0.01',
      fee_preview: null,
    },
  ],
} satisfies MarketView

assert(getPrimaryTokenView(market)?.token_id === 'no-token', 'NO token should be the primary token view')
assert(getOutcomeTokenView(market, 'YES')?.token_id === 'yes-token', 'YES token view should be resolved case-insensitively')
assert(getMarketTokenId(market) === 'no-token', 'market token id should use the primary token view')
assert(getMarketPosition(market)?.token_id === 'no-token', 'market position should come from token views')
assert(getMarketOpenOrderCount(market) === 1, 'open order count should be summed from token views')
assert(hasOpenSellOrders(market), 'sell orders should be detected from token views')
