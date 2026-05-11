import { create } from 'zustand'
import type { QueryCache } from '@tanstack/react-query'

// 前端自观测：用 React Query 的 QueryCache 订阅器统计请求量 / 错误数 / 最近成功时刻。
// 在 Providers 启动时通过 attachQueryStats(queryCache) 接入；HealthPage 读 store 渲染。

export type ApiHealthState = {
  totalFetches: number
  errorCount: number
  lastSuccessAt: number | null
  lastErrorAt: number | null
  lastErrorMessage: string | null
  /** 用于"近 N 分钟错误率"——内存里只滚动保留近 200 条记录。 */
  recent: Array<{ at: number; ok: boolean }>
  reset: () => void
}

const RECENT_MAX = 200

export const useApiHealthStore = create<ApiHealthState>((set) => ({
  totalFetches: 0,
  errorCount: 0,
  lastSuccessAt: null,
  lastErrorAt: null,
  lastErrorMessage: null,
  recent: [],
  reset: () =>
    set({
      totalFetches: 0,
      errorCount: 0,
      lastSuccessAt: null,
      lastErrorAt: null,
      lastErrorMessage: null,
      recent: [],
    }),
}))

function pushRecord(ok: boolean, errMsg: string | null) {
  const now = Date.now()
  useApiHealthStore.setState((state) => {
    const next = [...state.recent, { at: now, ok }].slice(-RECENT_MAX)
    return {
      totalFetches: state.totalFetches + 1,
      errorCount: ok ? state.errorCount : state.errorCount + 1,
      lastSuccessAt: ok ? now : state.lastSuccessAt,
      lastErrorAt: ok ? state.lastErrorAt : now,
      lastErrorMessage: ok ? state.lastErrorMessage : errMsg ?? '(unknown)',
      recent: next,
    }
  })
}

export function attachQueryStats(cache: QueryCache): () => void {
  // 订阅 QueryCache 的所有事件；只在 fetch 状态切回 'idle' 时计数（即真正完成）。
  return cache.subscribe((event) => {
    if (event.type !== 'updated') return
    const action = event.action
    if (action.type !== 'success' && action.type !== 'error') return
    if (action.type === 'success') {
      pushRecord(true, null)
    } else {
      const err = action.error
      const msg =
        err && typeof err === 'object' && 'message' in err && typeof err.message === 'string'
          ? err.message
          : String(err)
      pushRecord(false, msg)
    }
  })
}

/**
 * 给定 now（用 useState/setInterval 维护的 tick）做计算——纯函数，让组件可 memoize。
 * 不在内部调 Date.now()：调用方负责传入"当下"。
 */
export function recentErrorRateAt(
  state: ApiHealthState,
  windowMs: number,
  now: number,
): number | null {
  if (state.recent.length === 0) return null
  const cutoff = now - windowMs
  const recent = state.recent.filter((r) => r.at >= cutoff)
  if (recent.length === 0) return null
  const errors = recent.filter((r) => !r.ok).length
  return errors / recent.length
}

/** 仅 HealthPage 使用——不 export 防止意外外部依赖。 */
function _recentSampleCountAt(
  recent: Array<{ at: number; ok: boolean }>,
  windowMs: number,
  now: number,
): number {
  const cutoff = now - windowMs
  return recent.reduce((acc, r) => (r.at >= cutoff ? acc + 1 : acc), 0)
}

export { _recentSampleCountAt as recentSampleCountAt }
