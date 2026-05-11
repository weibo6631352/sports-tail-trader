/* eslint-disable react-refresh/only-export-components */
// 同文件导出 Provider + hooks 是 React 习惯——fast-refresh 提示忽略。
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { SseManager } from './manager'
import type { SseEvent, SseFilters, SseStatus } from './types'

const SseManagerContext = createContext<SseManager | null>(null)

export function SseProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient()
  const manager = useMemo(() => new SseManager(client), [client])

  useEffect(() => {
    manager.start()
    return () => manager.stop()
  }, [manager])

  return <SseManagerContext.Provider value={manager}>{children}</SseManagerContext.Provider>
}

export function useSseManager(): SseManager {
  const manager = useContext(SseManagerContext)
  if (!manager) throw new Error('useSseManager 必须在 SseProvider 内调用')
  return manager
}

export function useSseStatus(): SseStatus {
  const manager = useSseManager()
  const [status, setStatus] = useState<SseStatus>(() => manager.getStatus())
  useEffect(() => manager.onStatus(setStatus), [manager])
  return status
}

// 抽屉 / timeline 用：挂一条带 condition_id 过滤的辅助连接，组件卸载自动 close。
// listener 通过 ref 间接调用——parent 每次重渲染生成的新 listener 都能拿到最新闭包，
// 避免传统"listener 在 effect 挂载时被冻结"的 stale closure 陷阱。
export function useFilteredSse(
  filters: SseFilters,
  listener: (event: SseEvent) => void,
  deps: readonly unknown[] = [],
): SseStatus | null {
  const manager = useSseManager()
  const [status, setStatus] = useState<SseStatus | null>(null)
  const listenerRef = useRef(listener)
  // 用 useEffect 同步最新 listener 到 ref（React Compiler 不允许 render 期写 ref）。
  // 订阅本身在下面的 effect 里读 ref.current，永远拿到最新闭包。
  useEffect(() => {
    listenerRef.current = listener
  })

  useEffect(() => {
    const handle = manager.subscribeFiltered(filters, (event) => listenerRef.current(event))
    const tick = window.setInterval(() => setStatus(handle.status()), 1_000)
    return () => {
      window.clearInterval(tick)
      handle.close()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manager, filters.conditionId, JSON.stringify(filters.eventTypes), ...deps])
  return status
}
