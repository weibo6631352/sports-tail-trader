import { create } from 'zustand'

// 分析页统一时间窗口。
// since/until 一律 epoch_ms，对齐后端 build_time_range；undefined 表示"开放端"。
// 默认窗口 24h，对齐 backend analytics 默认。

export type TimeWindow = {
  since: number | null
  until: number | null
}

export type TimeWindowPreset =
  | 'last_1h'
  | 'last_6h'
  | 'last_24h'
  | 'last_7d'
  | 'last_30d'
  | 'custom'

export type TimeWindowState = TimeWindow & {
  preset: TimeWindowPreset
  setPreset: (preset: TimeWindowPreset) => void
  setCustom: (since: number | null, until: number | null) => void
  reset: () => void
}

const HOUR = 3_600_000
const DAY = 24 * HOUR

function rangeForPreset(preset: TimeWindowPreset): TimeWindow {
  if (preset === 'custom') return { since: null, until: null }
  const now = Date.now()
  if (preset === 'last_1h') return { since: now - HOUR, until: now }
  if (preset === 'last_6h') return { since: now - 6 * HOUR, until: now }
  if (preset === 'last_24h') return { since: now - DAY, until: now }
  if (preset === 'last_7d') return { since: now - 7 * DAY, until: now }
  return { since: now - 30 * DAY, until: now }
}

const DEFAULT_PRESET: TimeWindowPreset = 'last_24h'
const initial: TimeWindow = rangeForPreset(DEFAULT_PRESET)

export const useTimeWindowStore = create<TimeWindowState>((set) => ({
  preset: DEFAULT_PRESET,
  since: initial.since,
  until: initial.until,
  setPreset: (preset) => {
    const range = rangeForPreset(preset)
    set({ preset, since: range.since, until: range.until })
  },
  setCustom: (since, until) => set({ preset: 'custom', since, until }),
  reset: () => {
    const range = rangeForPreset(DEFAULT_PRESET)
    set({ preset: DEFAULT_PRESET, since: range.since, until: range.until })
  },
}))

export function windowMs(window: TimeWindow): number | null {
  if (window.since === null || window.until === null) return null
  return Math.max(0, window.until - window.since)
}
