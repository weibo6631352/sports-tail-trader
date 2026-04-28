import type { ReactNode } from 'react'
import { EmptyState } from './EmptyState'

export interface DataColumn<T> {
  key: string
  header: string
  align?: 'left' | 'right'
  cell: (row: T) => ReactNode
}

interface DataTableProps<T> {
  columns: DataColumn<T>[]
  rows: T[]
  rowKey: (row: T) => string
  emptyTitle: string
  emptyDescription: string
  onRowClick?: (row: T) => void
  selectedRowKey?: string | null
}

export const DataTable = <T,>({
  columns,
  rows,
  rowKey,
  emptyTitle,
  emptyDescription,
  onRowClick,
  selectedRowKey,
}: DataTableProps<T>) => {
  if (rows.length === 0) {
    return <EmptyState title={emptyTitle} description={emptyDescription} />
  }

  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key} className={column.align === 'right' ? 'align-right' : undefined}>
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const key = rowKey(row)
            return (
              <tr
                key={key}
                className={[
                  selectedRowKey === key ? 'is-selected' : null,
                  onRowClick ? 'is-clickable' : null,
                ]
                  .filter(Boolean)
                  .join(' ') || undefined}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
              >
                {columns.map((column) => (
                  <td
                    key={column.key}
                    className={column.align === 'right' ? 'align-right' : undefined}
                    data-label={column.header}
                  >
                    {column.cell(row)}
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
