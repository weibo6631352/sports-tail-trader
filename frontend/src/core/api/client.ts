import { ApiError } from './errors'

const API_BASE = '/api'

export type QueryValue = string | number | boolean | null | undefined
export type QueryParams = Record<string, QueryValue | QueryValue[]>

/**
 * 所有 HTTP 方法统一对象参数：未来给任何方法加 params / signal / headers 时
 * 不需要改签名，callsite 也不会因位置参数搬位而摔。
 */
export type RequestOptions = {
  params?: QueryParams
  body?: unknown
  signal?: AbortSignal
}

function buildUrl(path: string, params?: QueryParams): string {
  const url = `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`
  if (!params) return url
  const query = new URLSearchParams()
  for (const [key, raw] of Object.entries(params)) {
    if (raw === undefined || raw === null) continue
    if (Array.isArray(raw)) {
      for (const v of raw) {
        if (v === undefined || v === null) continue
        query.append(key, String(v))
      }
    } else {
      query.append(key, String(raw))
    }
  }
  const qs = query.toString()
  return qs ? `${url}?${qs}` : url
}

async function parseError(response: Response, url: string): Promise<ApiError> {
  let detail: unknown = null
  let message = response.statusText || 'request failed'
  const contentType = response.headers.get('content-type') ?? ''
  try {
    if (contentType.includes('application/json')) {
      const body = await response.json()
      detail = body
      if (body && typeof body === 'object' && 'detail' in (body as Record<string, unknown>)) {
        const inner = (body as Record<string, unknown>).detail
        if (typeof inner === 'string') message = inner
      }
    } else {
      const text = await response.text()
      detail = text
      if (text) message = text.slice(0, 200)
    }
  } catch {
    // 服务端返回了无法解析的体，保留 statusText 兜底信息。
  }
  const retryAfter = response.headers.get('retry-after')
  const retryAfterSeconds = retryAfter ? Number(retryAfter) : null
  return new ApiError({
    status: response.status,
    detail,
    message,
    url,
    retryAfterSeconds: Number.isFinite(retryAfterSeconds) ? retryAfterSeconds : null,
  })
}

async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const url = buildUrl(path, options.params)
  const headers: Record<string, string> = {}
  let body: BodyInit | undefined
  if (options.body !== undefined) {
    headers['content-type'] = 'application/json'
    body = JSON.stringify(options.body)
  }
  const response = await fetch(url, { method, headers, body, signal: options.signal })
  if (!response.ok) throw await parseError(response, url)
  if (response.status === 204) return undefined as T
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) {
    return (await response.json()) as T
  }
  return (await response.text()) as unknown as T
}

export const apiClient = {
  get<T>(path: string, options: RequestOptions = {}): Promise<T> {
    return request<T>('GET', path, options)
  },
  post<T>(path: string, options: RequestOptions = {}): Promise<T> {
    return request<T>('POST', path, options)
  },
  put<T>(path: string, options: RequestOptions = {}): Promise<T> {
    return request<T>('PUT', path, options)
  },
  // 命名为 del 避开 JS 关键字；后端 /parameters/{scope}/{key} 用 DELETE + body。
  del<T>(path: string, options: RequestOptions = {}): Promise<T> {
    return request<T>('DELETE', path, options)
  },
}

export function buildExportDownloadUrl(
  resource: 'orders' | 'fills' | 'audit_events',
  params: { format: 'csv' | 'jsonl'; since?: number; until?: number; limit?: number },
): string {
  return buildUrl(`/exports/${resource}`, params)
}
