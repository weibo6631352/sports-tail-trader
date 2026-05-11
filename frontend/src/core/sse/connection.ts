import type {
  SseConnectionState,
  SseEvent,
  SseFilters,
  SseListener,
  SseStatus,
  SseStatusListener,
} from './types'

// 指数退避：每次重连后 1s → 2s → 5s → 15s → 30s 封顶。
const BACKOFF_SCHEDULE_MS = [1_000, 2_000, 5_000, 15_000, 30_000]
const HEARTBEAT_MS = 15_000
// 速率限制兜底：后端 429 时退化为周期性 invalidate，避免 SSE 通道堵塞。
const RATE_LIMIT_FALLBACK_MS = 60_000

function buildStreamUrl(filters: SseFilters): string {
  const params = new URLSearchParams()
  if (filters.eventTypes && filters.eventTypes.length > 0) {
    params.set('event_types', filters.eventTypes.join(','))
  }
  if (filters.conditionId) params.set('condition_id', filters.conditionId)
  params.set('heartbeat_ms', String(HEARTBEAT_MS))
  const qs = params.toString()
  return `/stream/events${qs ? `?${qs}` : ''}`
}

export class SseConnection {
  private source: EventSource | null = null
  private listeners = new Set<SseListener>()
  private statusListeners = new Set<SseStatusListener>()
  private reconnectTimer: number | null = null
  private fallbackTimer: number | null = null
  private explicitlyClosed = false
  private status: SseStatus
  private readonly filters: SseFilters

  constructor(filters: SseFilters = {}) {
    this.filters = filters
    this.status = {
      state: 'idle',
      url: null,
      lastEventAt: null,
      lastErrorAt: null,
      lastError: null,
      reconnectAttempts: 0,
      retryAfterSeconds: null,
      droppedEventsTotal: 0,
      filters,
    }
  }

  getStatus(): SseStatus {
    return this.status
  }

  onEvent(listener: SseListener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  onStatus(listener: SseStatusListener): () => void {
    this.statusListeners.add(listener)
    listener(this.status)
    return () => this.statusListeners.delete(listener)
  }

  start(): void {
    if (this.source || this.explicitlyClosed) {
      if (this.explicitlyClosed) this.explicitlyClosed = false
    }
    if (this.source) return
    this.open()
  }

  close(): void {
    this.explicitlyClosed = true
    this.clearReconnect()
    this.clearFallback()
    if (this.source) {
      this.source.close()
      this.source = null
    }
    this.setState('closed')
  }

  private open(): void {
    const url = buildStreamUrl(this.filters)
    this.setState('connecting', { url })
    try {
      const source = new EventSource(url)
      this.source = source
      source.onopen = () => this.handleOpen()
      source.onmessage = (ev) => this.handleMessage(ev)
      source.onerror = () => this.handleError()
      source.addEventListener('ready', (ev) => this.handleNamedEvent('ready', ev as MessageEvent))
      source.addEventListener('subscription_lag', (ev) =>
        this.handleNamedEvent('subscription_lag', ev as MessageEvent),
      )
    } catch (err) {
      this.handleFailure(err instanceof Error ? err.message : String(err))
    }
  }

  private handleOpen(): void {
    this.setState('open', { reconnectAttempts: 0, retryAfterSeconds: null, lastError: null })
  }

  private handleMessage(ev: MessageEvent<string>): void {
    const payload = parseEventData(ev.data)
    const eventType =
      typeof payload['event_type'] === 'string' ? (payload['event_type'] as string) : 'unknown'
    this.dispatch({ eventType, payload, channel: 'message', receivedAt: Date.now() })
  }

  private handleNamedEvent(channel: 'ready' | 'subscription_lag', ev: MessageEvent<string>): void {
    const payload = parseEventData(ev.data)
    if (channel === 'subscription_lag') {
      const dropped = Number(payload['dropped'])
      if (Number.isFinite(dropped)) {
        this.status = { ...this.status, droppedEventsTotal: this.status.droppedEventsTotal + dropped }
        this.emitStatus()
      }
    }
    const eventType = channel
    this.dispatch({ eventType, payload, channel, receivedAt: Date.now() })
  }

  private handleError(): void {
    // EventSource 不暴露 HTTP 状态码；429 由 fetch 旁路探测兜底（fallbackProbe）。
    // 这里只做指数退避重连，并在连接失败时记录时间。
    this.handleFailure('event_source_error')
  }

  private handleFailure(message: string): void {
    if (this.source) {
      this.source.close()
      this.source = null
    }
    this.setState('reconnecting', {
      lastError: message,
      lastErrorAt: Date.now(),
    })
    if (this.explicitlyClosed) return
    this.scheduleReconnect()
    this.probeForRateLimit()
  }

  private scheduleReconnect(): void {
    const attempt = Math.min(this.status.reconnectAttempts, BACKOFF_SCHEDULE_MS.length - 1)
    const delay = BACKOFF_SCHEDULE_MS[attempt]
    this.clearReconnect()
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.status = {
        ...this.status,
        reconnectAttempts: this.status.reconnectAttempts + 1,
      }
      this.open()
    }, delay)
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private clearFallback(): void {
    if (this.fallbackTimer !== null) {
      window.clearTimeout(this.fallbackTimer)
      this.fallbackTimer = null
    }
  }

  // 用 HEAD 探测一次 /stream/events；命中 429 时切到 rate_limited 状态，并启用
  // RATE_LIMIT_FALLBACK_MS 周期的"重试连接"循环。不在 dispatch 链路内 await。
  private probeForRateLimit(): void {
    if (this.explicitlyClosed) return
    fetch(buildStreamUrl(this.filters), { method: 'GET', cache: 'no-store' })
      .then(async (response) => {
        if (response.status === 429) {
          const retryAfter = Number(response.headers.get('retry-after')) || RATE_LIMIT_FALLBACK_MS / 1000
          this.setState('rate_limited', {
            retryAfterSeconds: retryAfter,
            lastError: 'sse_subscriber_cap_exceeded',
            lastErrorAt: Date.now(),
          })
          this.clearReconnect()
          this.clearFallback()
          this.fallbackTimer = window.setTimeout(() => {
            this.fallbackTimer = null
            this.open()
          }, retryAfter * 1000)
        }
        // 探测请求会立即返回响应体（SSE 头），主动 cancel 不留连接。
        await response.body?.cancel().catch(() => undefined)
      })
      .catch(() => {
        // 探测失败不影响主重连节奏；交给上面的 schedule。
      })
  }

  private dispatch(event: SseEvent): void {
    this.status = { ...this.status, lastEventAt: event.receivedAt }
    this.emitStatus()
    for (const listener of this.listeners) {
      try {
        listener(event)
      } catch (err) {
        // 单个 listener 异常不影响其他订阅者；记一条 console 但不挂连接。
        console.error('[sse] listener error', err)
      }
    }
  }

  private setState(state: SseConnectionState, patch: Partial<SseStatus> = {}): void {
    this.status = { ...this.status, state, ...patch }
    this.emitStatus()
  }

  private emitStatus(): void {
    for (const listener of this.statusListeners) listener(this.status)
  }
}

function parseEventData(raw: string): Record<string, unknown> {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
    return { value: parsed }
  } catch {
    return { raw }
  }
}
