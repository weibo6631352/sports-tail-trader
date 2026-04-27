import { appEnv } from '../config/env'

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(message: string, status: number, detail: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> => {
  return typeof value === 'object' && value !== null
}

const buildUrl = (path: string): string => `${appEnv.apiBaseUrl}${path}`

export const buildSearch = (params: Record<string, unknown>): string => {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') {
      return
    }
    if (Array.isArray(value)) {
      value.forEach((item) => search.append(key, String(item)))
      return
    }
    search.set(key, String(value))
  })
  const serialized = search.toString()
  return serialized ? `?${serialized}` : ''
}

const parseErrorDetail = async (response: Response): Promise<unknown> => {
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) {
    return response.json()
  }
  return response.text()
}

export const formatApiError = (error: unknown): string => {
  if (error instanceof ApiError) {
    if (isRecord(error.detail)) {
      const nestedDetail = error.detail.detail
      if (typeof nestedDetail === 'string' && nestedDetail.trim() !== '') {
        return nestedDetail
      }
      if (Array.isArray(nestedDetail)) {
        const messages = nestedDetail
          .map((item) => {
            if (isRecord(item) && typeof item.msg === 'string') {
              return item.msg
            }
            return null
          })
          .filter((message): message is string => Boolean(message))
        if (messages.length > 0) {
          return messages.join('；')
        }
      }
    }
    return `${error.message} (${error.status})`
  }
  if (error instanceof Error) {
    return error.message
  }
  return '请求失败，请检查服务端日志。'
}

export const apiClient = {
  async get<T>(path: string): Promise<T> {
    const response = await fetch(buildUrl(path), {
      headers: {
        Accept: 'application/json',
      },
    })
    if (!response.ok) {
      throw new ApiError(`GET ${path} failed`, response.status, await parseErrorDetail(response))
    }
    return response.json() as Promise<T>
  },
  async post<T>(path: string, body: unknown): Promise<T> {
    const response = await fetch(buildUrl(path), {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    })
    if (!response.ok) {
      throw new ApiError(`POST ${path} failed`, response.status, await parseErrorDetail(response))
    }
    return response.json() as Promise<T>
  },
}
