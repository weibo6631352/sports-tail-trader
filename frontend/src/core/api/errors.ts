export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown
  readonly retryAfterSeconds: number | null
  readonly url: string

  constructor(params: {
    status: number
    detail: unknown
    message: string
    url: string
    retryAfterSeconds?: number | null
  }) {
    super(params.message)
    this.name = 'ApiError'
    this.status = params.status
    this.detail = params.detail
    this.url = params.url
    this.retryAfterSeconds = params.retryAfterSeconds ?? null
  }

  isClientError(): boolean {
    return this.status >= 400 && this.status < 500
  }

  isRateLimited(): boolean {
    return this.status === 429
  }

  isUnavailable(): boolean {
    return this.status === 503
  }
}

export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    const detail =
      typeof err.detail === 'string'
        ? err.detail
        : err.detail && typeof err.detail === 'object'
          ? JSON.stringify(err.detail)
          : ''
    return detail ? `${err.status} ${err.message} · ${detail}` : `${err.status} ${err.message}`
  }
  if (err instanceof Error) return err.message
  return String(err)
}
