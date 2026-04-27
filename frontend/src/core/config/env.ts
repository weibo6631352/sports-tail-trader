const trimTrailingSlash = (value: string): string => value.replace(/\/+$/, '')
const defaultApiBaseUrl = import.meta.env.DEV ? 'http://127.0.0.1:8000' : '/api'

export const appEnv = {
  apiBaseUrl: trimTrailingSlash(import.meta.env.VITE_API_BASE_URL?.trim() || defaultApiBaseUrl),
}
