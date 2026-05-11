import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
  type Row,
  type Table,
} from '@tanstack/react-table'
import { ActionIcon, Group, Pagination, Skeleton, Stack, Text } from '@mantine/core'
import { IconRefresh } from '@tabler/icons-react'
import type { ReactNode } from 'react'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import styles from './DataTable.module.css'

export type DataTableProps<T> = {
  columns: ColumnDef<T, unknown>[]
  data: T[] | undefined
  isLoading?: boolean
  isFetching?: boolean
  error?: unknown
  emptyTitle?: string
  emptyDescription?: ReactNode
  onRefresh?: () => void
  onRowClick?: (row: Row<T>) => void
  rowKey?: (row: T, index: number) => string
  pagination?: {
    page: number
    pageCount: number
    onChange: (page: number) => void
  }
  rightToolbar?: ReactNode
  density?: 'compact' | 'comfortable'
}

export function DataTable<T>({
  columns,
  data,
  isLoading,
  isFetching,
  error,
  emptyTitle,
  emptyDescription,
  onRefresh,
  onRowClick,
  rowKey,
  pagination,
  rightToolbar,
  density = 'compact',
}: DataTableProps<T>) {
  const table: Table<T> = useReactTable({
    data: data ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
  })

  return (
    <Stack gap="xs">
      <Group justify="space-between" align="center">
        <Group gap="xs">
          {pagination ? (
            <Pagination
              size="xs"
              value={pagination.page}
              total={Math.max(1, pagination.pageCount)}
              onChange={pagination.onChange}
              withEdges
            />
          ) : null}
          {isFetching && !isLoading ? (
            <Text size="xs" c="dimmed">
              同步中…
            </Text>
          ) : null}
        </Group>
        <Group gap="xs">
          {rightToolbar}
          {onRefresh ? (
            <ActionIcon variant="subtle" onClick={onRefresh} aria-label="刷新">
              <IconRefresh size={14} />
            </ActionIcon>
          ) : null}
        </Group>
      </Group>

      {error ? (
        <QueryErrorNotice error={error} onRetry={onRefresh} />
      ) : (
        <div className={styles.scroll} data-density={density}>
          <table className={styles.table}>
            <thead>
              {table.getHeaderGroups().map((headerGroup) => (
                <tr key={headerGroup.id}>
                  {headerGroup.headers.map((header) => (
                    <th key={header.id} style={{ width: header.column.columnDef.size }}>
                      {header.isPlaceholder
                        ? null
                        : flexRender(header.column.columnDef.header, header.getContext())}
                    </th>
                  ))}
                </tr>
              ))}
            </thead>
            <tbody>
              {isLoading
                ? Array.from({ length: 6 }).map((_, idx) => (
                    <tr key={`skel-${idx}`}>
                      {columns.map((_col, colIdx) => (
                        <td key={colIdx}>
                          <Skeleton h={14} />
                        </td>
                      ))}
                    </tr>
                  ))
                : table.getRowModel().rows.map((row, idx) => (
                    <tr
                      key={rowKey ? rowKey(row.original, idx) : row.id}
                      onClick={onRowClick ? () => onRowClick(row) : undefined}
                      data-clickable={onRowClick ? 'true' : undefined}
                    >
                      {row.getVisibleCells().map((cell) => (
                        <td key={cell.id}>
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </td>
                      ))}
                    </tr>
                  ))}
            </tbody>
          </table>
          {!isLoading && (data?.length ?? 0) === 0 ? (
            <EmptyState title={emptyTitle ?? '暂无数据'} description={emptyDescription} />
          ) : null}
        </div>
      )}
    </Stack>
  )
}
