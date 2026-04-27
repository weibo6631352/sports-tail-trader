import type { JsonValue } from '../../core/api/types'

const numberFormatter = new Intl.NumberFormat('zh-CN', {
  maximumFractionDigits: 4,
})

const compactFormatter = new Intl.NumberFormat('zh-CN', {
  notation: 'compact',
  maximumFractionDigits: 2,
})

const unlimitedAllowanceThreshold = 1_000_000_000_000

export const formatDecimal = (value: string | number | null | undefined): string => {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  const numericValue = Number(value)
  if (Number.isNaN(numericValue)) {
    return String(value)
  }
  return numberFormatter.format(numericValue)
}

export const formatCompact = (value: string | number | null | undefined): string => {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  const numericValue = Number(value)
  if (Number.isNaN(numericValue)) {
    return String(value)
  }
  return compactFormatter.format(numericValue)
}

export const formatAllowance = (value: string | number | null | undefined): string => {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  const numericValue = Number(value)
  if (Number.isNaN(numericValue)) {
    return String(value)
  }
  if (numericValue >= unlimitedAllowanceThreshold) {
    return '无限授权'
  }
  return numberFormatter.format(numericValue)
}

export const formatDateTime = (value: string | null | undefined): string => {
  if (!value) {
    return '—'
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return value
  }
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

export const formatFullDateTime = (value: string | null | undefined): string => {
  if (!value) {
    return '—'
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return value
  }
  const parts = new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZoneName: 'short',
  }).formatToParts(date)
  const partMap = Object.fromEntries(
    parts
      .filter((part) => part.type !== 'literal')
      .map((part) => [part.type, part.value]),
  )
  const timeZoneName = partMap.timeZoneName ? ` ${partMap.timeZoneName}` : ''
  return `${partMap.year}/${partMap.month}/${partMap.day} ${partMap.hour}:${partMap.minute}${timeZoneName}`
}

export const formatAddressShort = (value: string | null | undefined): string => {
  if (!value) {
    return '—'
  }
  const normalized = value.trim()
  if (normalized.length <= 16) {
    return normalized
  }
  return `${normalized.slice(0, 6)}...${normalized.slice(-4)}`
}

export const formatBool = (value: boolean | null | undefined): string => {
  if (value === null || value === undefined) {
    return '—'
  }
  return value ? '是' : '否'
}

export const formatJson = (value: JsonValue | unknown): string => {
  if (value === undefined) {
    return '—'
  }
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

export const formatList = (values: Array<string | null | undefined>): string => {
  const items = values.filter((value): value is string => Boolean(value))
  return items.length > 0 ? items.join(' / ') : '—'
}

export const getString = (value: unknown): string | null => {
  return typeof value === 'string' ? value : null
}
