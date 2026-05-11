import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Checkbox, Group, TextInput } from '@mantine/core'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { ordersApi } from '@core/api/resources'
import type { OrderRow } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { CopyableId } from '@shared/ui/CopyableId'
import { StatusPill } from '@shared/ui/StatusPill'
import { DataTable } from '@shared/tables/DataTable'
import { formatDecimal, formatIso } from '@shared/format'

const PAGE_SIZE = 100

export function OrdersPage() {
  const [page, setPage] = useState(1)
  const [openOnly, setOpenOnly] = useState(false)
  const [traceId, setTraceId] = useState('')
  const [conditionId, setConditionId] = useState('')

  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      open_only: openOnly,
      trace_id: traceId.trim() || undefined,
      condition_id: conditionId.trim() || undefined,
    }),
    [page, openOnly, traceId, conditionId],
  )

  const query = useQuery({ queryKey: qk.orders.list(params), queryFn: ({ signal }) => ordersApi.list(params, signal) })

  const columns: ColumnDef<OrderRow, unknown>[] = [
    { header: 'order_id', cell: ({ row }) => <CopyableId value={row.original.order_id} /> },
    {
      header: 'when',
      cell: ({ row }) => (
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11 }}>
          {formatIso(row.original.created_at, 'MM-DD HH:mm:ss')}
        </span>
      ),
    },
    { header: 'market', accessorKey: 'market_slug' },
    {
      header: 'side',
      cell: ({ row }) => (
        <StatusPill tone={row.original.side === 'BUY' ? 'success' : 'warning'} size="xs">
          {row.original.side}
        </StatusPill>
      ),
    },
    { header: 'price', cell: ({ row }) => formatDecimal(row.original.price, { dp: 4 }) },
    {
      header: 'filled / size',
      cell: ({ row }) =>
        `${formatDecimal(row.original.filled_shares, { dp: 2 })} / ${formatDecimal(row.original.size_shares, { dp: 2 })}`,
    },
    { header: 'status', cell: ({ row }) => <StatusPill size="xs">{row.original.status}</StatusPill> },
    { header: 'trace', cell: ({ row }) => <CopyableId value={row.original.trace_id ?? ''} dense /> },
  ]

  const total = query.data?.total ?? query.data?.items?.length ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <>
      <PageHeader
        title="订单 Orders"
        subtitle="历史全量；replace / cancel 操作走 /live/orders 入口（避免重复表单）"
      />
      <Group gap="xs" mb="sm" wrap="wrap">
        <Checkbox
          size="xs"
          label="open_only"
          checked={openOnly}
          onChange={(e) => setOpenOnly(e.currentTarget.checked)}
        />
        <TextInput size="xs" placeholder="trace_id" value={traceId} onChange={(e) => setTraceId(e.currentTarget.value)} w={300} />
        <TextInput size="xs" placeholder="condition_id" value={conditionId} onChange={(e) => setConditionId(e.currentTarget.value)} w={320} />
      </Group>
      <DataTable<OrderRow>
        columns={columns}
        data={query.data?.items}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        pagination={{ page, pageCount: totalPages, onChange: setPage }}
        rowKey={(o) => o.order_id}
      />
    </>
  )
}
