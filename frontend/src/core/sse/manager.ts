import type { QueryClient } from '@tanstack/react-query'
import { SseConnection } from './connection'
import { DEFAULT_EVENT_ROUTES, dispatchEvent, type EventRoute } from './eventRouter'
import type { SseEvent, SseFilters, SseStatus } from './types'

// 全局 SSE 编排：
//   - 主连接：无过滤，订阅全量 → 失效任意页 query
//   - 局部连接（per condition_id+eventTypes）：抽屉 / timeline 打开时挂一条带过滤的
//     辅助连接，减少应用层无关事件
//
// 连接池：同 (eventTypes hash, conditionId) 的订阅者共享一条 EventSource。
// 多个组件同时看同一市场（drawer + timeline 等）只占一个后端 subscriber slot；
// refcount 归零才真正关连接。

type SubscriptionHandle = { close: () => void; status: () => SseStatus }

type PooledConnection = {
  conn: SseConnection
  listeners: Set<(event: SseEvent) => void>
}

function poolKey(filters: SseFilters): string {
  const conditionId = filters.conditionId ?? ''
  // 排序保证 ['a','b'] 与 ['b','a'] 同 key——后端语义对顺序不敏感。
  const types = filters.eventTypes ? [...filters.eventTypes].sort().join(',') : ''
  return `${conditionId}|${types}`
}

export class SseManager {
  private primary: SseConnection
  private routes: EventRoute[]
  private statusSubscribers = new Set<(status: SseStatus) => void>()
  private currentStatus: SseStatus
  private readonly client: QueryClient
  private readonly pool = new Map<string, PooledConnection>()

  constructor(client: QueryClient, routes: EventRoute[] = DEFAULT_EVENT_ROUTES) {
    this.client = client
    this.routes = routes
    this.primary = new SseConnection({})
    this.currentStatus = this.primary.getStatus()
    this.primary.onEvent((event) => dispatchEvent(this.client, event, this.routes))
    this.primary.onStatus((status) => {
      this.currentStatus = status
      for (const listener of this.statusSubscribers) listener(status)
    })
  }

  start(): void {
    this.primary.start()
  }

  stop(): void {
    this.primary.close()
    // 关闭主连接的同时把所有过滤连接也回收，避免 Provider 重建后泄漏。
    for (const pooled of this.pool.values()) {
      pooled.conn.close()
    }
    this.pool.clear()
  }

  onStatus(listener: (status: SseStatus) => void): () => void {
    this.statusSubscribers.add(listener)
    listener(this.currentStatus)
    return () => this.statusSubscribers.delete(listener)
  }

  getStatus(): SseStatus {
    return this.currentStatus
  }

  // 同 filters 复用一条连接；refcount 归零才真正关。
  subscribeFiltered(filters: SseFilters, listener: (event: SseEvent) => void): SubscriptionHandle {
    const key = poolKey(filters)
    let pooled = this.pool.get(key)
    if (!pooled) {
      const conn = new SseConnection(filters)
      const listeners = new Set<(event: SseEvent) => void>()
      pooled = { conn, listeners }
      this.pool.set(key, pooled)
      // 池里的事件 fan-out 到所有 listener；单个 listener 异常不影响其他。
      conn.onEvent((event) => {
        for (const l of listeners) {
          try {
            l(event)
          } catch (err) {
            console.error('[sse-pool] listener error', err)
          }
        }
      })
      conn.start()
    }
    pooled.listeners.add(listener)
    const handle: SubscriptionHandle = {
      close: () => {
        if (!pooled) return
        pooled.listeners.delete(listener)
        if (pooled.listeners.size === 0) {
          pooled.conn.close()
          this.pool.delete(key)
        }
      },
      status: () => pooled!.conn.getStatus(),
    }
    return handle
  }
}
