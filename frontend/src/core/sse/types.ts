// SSE 通用类型：连接状态、订阅句柄、入站事件。

export type SseConnectionState =
  | 'idle'
  | 'connecting'
  | 'open'
  | 'reconnecting'
  | 'rate_limited'
  | 'closed'

export type SseFilters = {
  eventTypes?: readonly string[]
  conditionId?: string
}

export type SseEvent = {
  /** 后端 event_type 字段；为空时回退到 raw payload */
  eventType: string
  /** 原始解析后的 payload；UI 路由按需投影 */
  payload: Record<string, unknown>
  /** EventSource native event 类型：默认 'message' 或 ready/subscription_lag/heartbeat */
  channel: 'message' | 'ready' | 'subscription_lag' | string
  /** 解析时刻；不是后端 ts */
  receivedAt: number
}

export type SseStatus = {
  state: SseConnectionState
  url: string | null
  lastEventAt: number | null
  lastErrorAt: number | null
  lastError: string | null
  reconnectAttempts: number
  retryAfterSeconds: number | null
  droppedEventsTotal: number
  filters: SseFilters
}

export type SseListener = (event: SseEvent) => void
export type SseStatusListener = (status: SseStatus) => void
export type SseUnsubscribe = () => void
