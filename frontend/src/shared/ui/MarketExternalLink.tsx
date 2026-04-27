import type { MouseEventHandler, ReactNode } from 'react'
import { buildPolymarketEventUrl } from '../utils/markets'

interface MarketExternalLinkProps {
  eventSlug?: string | null
  className?: string
  onClick?: MouseEventHandler<HTMLElement>
  children: ReactNode
}

export const MarketExternalLink = ({
  eventSlug,
  className,
  onClick,
  children,
}: MarketExternalLinkProps) => {
  const url = buildPolymarketEventUrl(eventSlug)
  if (!url) {
    return (
      <span className={className} onClick={onClick}>
        {children}
      </span>
    )
  }
  return (
    <a href={url} target="_blank" rel="noreferrer" className={className} onClick={onClick}>
      {children}
    </a>
  )
}
