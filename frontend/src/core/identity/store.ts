import { create } from 'zustand'

// 操作者身份——所有写操作必须带 operator + reason。
// 默认值从 localStorage 取，并随后续修改回写，避免分析师每次手敲。

const STORAGE_KEY = 'stt:operator'

function loadInitial(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? 'manual'
  } catch {
    return 'manual'
  }
}

export type OperatorState = {
  operator: string
  setOperator: (value: string) => void
}

export const useOperatorStore = create<OperatorState>((set) => ({
  operator: loadInitial(),
  setOperator: (value) => {
    const next = value.trim() || 'manual'
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // 隐私模式 / 配额耗尽：写失败不影响内存值。
    }
    set({ operator: next })
  },
}))
