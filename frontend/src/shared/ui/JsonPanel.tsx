import type { ReactNode } from 'react'
import { formatJson } from '../utils/format'

interface JsonPanelProps {
  value: unknown
  emptyLabel?: string
  detailsLabel?: string
  defaultOpen?: boolean
  summary?: ReactNode
}

const isEmptyValue = (value: unknown): boolean => {
  if (value === null || value === undefined || value === '') {
    return true
  }
  if (Array.isArray(value)) {
    return value.length === 0
  }
  if (typeof value === 'object') {
    return Object.keys(value as Record<string, unknown>).length === 0
  }
  return false
}

export const JsonPanel = ({
  value,
  emptyLabel = '暂无数据。',
  detailsLabel = '查看原始数据',
  defaultOpen = false,
  summary,
}: JsonPanelProps) => {
  if (isEmptyValue(value)) {
    return <p className="muted">{emptyLabel}</p>
  }

  return (
    <div className="json-panel">
      {summary}
      <details className="json-panel__details" open={defaultOpen}>
        <summary>{detailsLabel}</summary>
        <pre className="json-block">{formatJson(value)}</pre>
      </details>
    </div>
  )
}
