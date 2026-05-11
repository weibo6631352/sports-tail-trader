import { create } from 'zustand'

// 分析页通用维度过滤器。所有分析路由共享：league / market_type / strategy_id。
// 变更不自动 refetch；页面里点"查询"按钮把当前值传到 query params。

export type AnalyticsFilters = {
  league: string | null
  marketType: string | null
  strategyId: string | null
}

export type AnalyticsFiltersState = AnalyticsFilters & {
  setLeague: (v: string | null) => void
  setMarketType: (v: string | null) => void
  setStrategyId: (v: string | null) => void
  reset: () => void
}

export const useAnalyticsFiltersStore = create<AnalyticsFiltersState>((set) => ({
  league: null,
  marketType: null,
  strategyId: null,
  setLeague: (v) => set({ league: v }),
  setMarketType: (v) => set({ marketType: v }),
  setStrategyId: (v) => set({ strategyId: v }),
  reset: () => set({ league: null, marketType: null, strategyId: null }),
}))
