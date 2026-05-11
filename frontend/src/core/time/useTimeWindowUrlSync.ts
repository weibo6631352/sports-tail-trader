import { useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useTimeWindowStore } from './store'

// TimeWindow ↔ URL search params (since / until) 双向同步。
// 挂载 TimeWindowPicker 的页面调用一次。
//
// 行为：
//   - 挂载时，URL 带 since/until → 读到 store（分享链接进入）
//     URL 不带 → 把当前 store 推到 URL（统一可分享）
//   - store 变更 → URL 同步更新（用户改 preset）
//   - URL 变更（后退 / 前进 / 编辑 URL 栏）→ store 同步更新
//
// 防循环：用 lastPushed ref 标记自己写出去的 URL；下一次 searchParams 变化时
// 比对——若与 ref 一致即"是我刚写的"，跳过；不一致才视作外部变更回灌 store。
// 不写 preset 字段（同样 since/until 可由多个 preset 重建，展示侧自适应）。

export function useTimeWindowUrlSync(): void {
  const [searchParams, setSearchParams] = useSearchParams()

  // 记录自己最近一次推到 URL 的值，用以区分"自写"与"外部变更"。
  const lastPushedRef = useRef<{ since: number | null; until: number | null } | null>(null)
  // 挂载时仅一次完成初始化——避免和后续 URL 同步 effect 双重写入。
  const initializedRef = useRef(false)

  // mount-only：初始化方向
  useEffect(() => {
    if (initializedRef.current) return
    initializedRef.current = true
    const params = new URLSearchParams(window.location.search)
    const urlSince = parseEpoch(params.get('since'))
    const urlUntil = parseEpoch(params.get('until'))
    if (urlSince !== null || urlUntil !== null) {
      lastPushedRef.current = { since: urlSince, until: urlUntil }
      useTimeWindowStore.getState().setCustom(urlSince, urlUntil)
    } else {
      const state = useTimeWindowStore.getState()
      lastPushedRef.current = { since: state.since, until: state.until }
      pushToUrl(state.since, state.until, setSearchParams)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // URL → store：每次 searchParams 变化都重检查；防自写循环靠 lastPushedRef。
  useEffect(() => {
    if (!initializedRef.current) return
    const urlSince = parseEpoch(searchParams.get('since'))
    const urlUntil = parseEpoch(searchParams.get('until'))
    const last = lastPushedRef.current
    if (last && last.since === urlSince && last.until === urlUntil) return
    const current = useTimeWindowStore.getState()
    if (current.since === urlSince && current.until === urlUntil) return
    lastPushedRef.current = { since: urlSince, until: urlUntil }
    useTimeWindowStore.getState().setCustom(urlSince, urlUntil)
  }, [searchParams])

  // store → URL：每次时间变化推到 URL，并记到 ref 让上面那个 effect 知道是自写。
  useEffect(() => {
    const unsubscribe = useTimeWindowStore.subscribe((state, prev) => {
      if (state.since === prev.since && state.until === prev.until) return
      lastPushedRef.current = { since: state.since, until: state.until }
      pushToUrl(state.since, state.until, setSearchParams)
    })
    return unsubscribe
  }, [setSearchParams])
}

function parseEpoch(raw: string | null): number | null {
  if (raw === null) return null
  const n = Number(raw)
  return Number.isFinite(n) && n >= 0 ? n : null
}

function pushToUrl(
  since: number | null,
  until: number | null,
  setSearchParams: ReturnType<typeof useSearchParams>[1],
): void {
  setSearchParams(
    (prev) => {
      const next = new URLSearchParams(prev)
      if (since !== null) next.set('since', String(since))
      else next.delete('since')
      if (until !== null) next.set('until', String(until))
      else next.delete('until')
      return next
    },
    { replace: true },
  )
}
