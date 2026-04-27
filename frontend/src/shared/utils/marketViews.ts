import type { MarketTokenView, MarketView, OrderRecord, PositionRecord } from '../../core/api/types'
import { hasPositiveShares } from './markets'
import { isSellOrderSide } from './labels'

const normalizeOutcome = (outcome: string): string => outcome.trim().toLowerCase()

export const getTokenViews = (market: MarketView): MarketTokenView[] => market.token_views ?? []

export const getOutcomeTokenView = (
  market: MarketView,
  outcome: string,
): MarketTokenView | null => {
  const normalizedOutcome = normalizeOutcome(outcome)
  return getTokenViews(market).find((view) => normalizeOutcome(view.outcome) === normalizedOutcome) ?? null
}

export const getPrimaryTokenView = (market: MarketView): MarketTokenView | null =>
  getOutcomeTokenView(market, 'NO') ?? getTokenViews(market)[0] ?? null

export const getSecondaryTokenView = (market: MarketView): MarketTokenView | null => {
  const primaryTokenId = getPrimaryTokenView(market)?.token_id
  return getOutcomeTokenView(market, 'YES') ?? getTokenViews(market).find((view) => view.token_id !== primaryTokenId) ?? null
}

export const getTokenViewById = (
  market: MarketView,
  tokenId: string | null | undefined,
): MarketTokenView | null => {
  if (!tokenId) {
    return null
  }
  return getTokenViews(market).find((view) => view.token_id === tokenId) ?? null
}

export const marketHasToken = (market: MarketView, tokenId: string | null | undefined): boolean =>
  getTokenViewById(market, tokenId) !== null

export const getMarketTokenId = (market: MarketView): string =>
  getPrimaryTokenView(market)?.token_id ?? market.market.token_ids[0] ?? market.market.condition_id

export const getMarketPosition = (market: MarketView): PositionRecord | null =>
  getTokenViews(market).find((view) => hasPositiveShares(view.position?.shares))?.position ??
  getPrimaryTokenView(market)?.position ??
  null

export const getMarketOpenOrders = (market: MarketView): OrderRecord[] =>
  getTokenViews(market).flatMap((view) => view.open_orders ?? [])

export const getMarketOpenOrderCount = (market: MarketView): number =>
  getTokenViews(market).reduce(
    (total, view) => total + (view.open_order_count ?? view.open_orders?.length ?? 0),
    0,
  )

export const hasOpenSellOrders = (market: MarketView): boolean =>
  getMarketOpenOrders(market).some((order) => isSellOrderSide(order.side))
