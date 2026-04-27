export const buildPolymarketEventUrl = (eventSlug: string | null | undefined): string | null => {
  const slug = eventSlug?.trim()
  if (!slug) {
    return null
  }
  return `https://polymarket.com/event/${encodeURIComponent(slug)}`
}

export const hasPositiveShares = (value: string | number | null | undefined): boolean => {
  if (value === null || value === undefined || value === '') {
    return false
  }
  const numericValue = Number(value)
  return Number.isFinite(numericValue) && numericValue > 0
}
