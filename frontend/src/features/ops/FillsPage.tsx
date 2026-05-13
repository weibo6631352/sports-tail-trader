import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, TextInput } from '@mantine/core'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { fillsApi } from '@core/api/resources'
import type { FillRow } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { CopyableId } from '@shared/ui/CopyableId'
import { DataTable } from '@shared/tables/DataTable'
import { formatDecimal, formatIso, formatUsdc } from '@shared/format'

const PAGE_SIZE = 100

export function FillsPage() {
  const [page, setPage] = useState(1)
  const [traceId, setTraceId] = useState('')
  const [conditionId, setConditionId] = useState('')

  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      trace_id: traceId.trim() || undefined,
      condition_id: conditionId.trim() || undefined,
    }),
    [page, traceId, conditionId],
  )
  const query = useQuery({ queryKey: qk.fills.list(params), queryFn: ({ signal }) => fillsApi.list(params, signal) })

  const columns: ColumnDef<FillRow, unknown>[] = [
    { header: 'when', cell: ({ row }) => formatIso(row.original.created_at, 'MM-DD HH:mm:ss') },
    {
      header: 'condition / token',
      cell: ({ row }) => (
        <div>
          {row.original.condition_id ? <CopyableId value={row.original.condition_id} dense /> : '—'}
          {row.original.token_id ? <CopyableId value={row.original.token_id} dense /> : null}
        </div>
      ),
    },
    { header: 'order_id', cell: ({ row }) => <CopyableId value={row.original.order_id ?? ''} dense /> },
    { header: 'side', accessorKey: 'side' },
    { header: 'price', cell: ({ row }) => formatDecimal(row.original.price, { dp: 4 }) },
    { header: 'size', cell: ({ row }) => formatDecimal(row.original.size, { dp: 2 }) },
    { header: 'notional', cell: ({ row }) => formatUsdc(row.original.notional_usdc) },
    { header: 'fee', cell: ({ row }) => formatUsdc(row.original.fee_usdc, 4) },
    { header: 'trace', cell: ({ row }) => <CopyableId value={row.original.trace_id ?? ''} dense /> },
  ]

  const total = query.data?.total ?? query.data?.items?.length ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <>
      <PageHeader title="成交流水 Fills" subtitle="按 trace_id / condition_id 过滤" />
      <Group gap="xs" mb="sm">
        <TextInput
          size="xs"
          placeholder="trace_id"
          value={traceId}
          onChange={(e) => { setTraceId(e.currentTarget.value); setPage(1) }}
          w={280}
        />
        <TextInput
          size="xs"
          placeholder="condition_id (0x...)"
          value={conditionId}
          onChange={(e) => { setConditionId(e.currentTarget.value); setPage(1) }}
          w={280}
        />
      </Group>
      <DataTable<FillRow>
        columns={columns}
        data={query.data?.items}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        pagination={{ page, pageCount: totalPages, onChange: setPage }}
        rowKey={(f) => f.fill_id}
      />
    </>
  )
}
