import { useMemo } from 'react'
import { useTimeWindowStore, windowMs } from '@core/time/store'
import { useAnalyticsFiltersStore } from '@core/filters/store'

// 通用：把 time + filters store 转成 API 参数对象。
export function useAnalyticsParams(): {
  window_ms?: number
  end_ms?: number
  since?: number
  until?: number
  league?: string
  market_type?: string
  strategy_id?: string
} {
  const since = useTimeWindowStore((s) => s.since)
  const until = useTimeWindowStore((s) => s.until)
  const league = useAnalyticsFiltersStore((s) => s.league)
  const marketType = useAnalyticsFiltersStore((s) => s.marketType)
  const strategyId = useAnalyticsFiltersStore((s) => s.strategyId)

  return useMemo(() => {
    const ms = windowMs({ since, until })
    return {
      window_ms: ms ?? undefined,
      end_ms: until ?? undefined,
      since: since ?? undefined,
      until: until ?? undefined,
      league: league ?? undefined,
      market_type: marketType ?? undefined,
      strategy_id: strategyId ?? undefined,
    }
  }, [since, until, league, marketType, strategyId])
}
