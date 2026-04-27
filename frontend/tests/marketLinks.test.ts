import { buildPolymarketEventUrl } from '../src/shared/utils/markets'

const assert = (condition: unknown, message: string): void => {
  if (!condition) {
    throw new Error(message)
  }
}

assert(
  buildPolymarketEventUrl('sample-event-a') === 'https://polymarket.com/event/sample-event-a',
  'event slug should build a Polymarket event URL',
)

assert(
  buildPolymarketEventUrl(null) === null,
  'missing event slug should not build a Polymarket event URL',
)
