import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, TextInput } from '@mantine/core'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { allocationsApi } from '@core/api/resources'
import type { AllocationRow } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { CopyableId } from '@shared/ui/CopyableId'
import { DataTable } from '@shared/tables/DataTable'
import { formatIso, formatUsdc } from '@shared/format'

const PAGE_SIZE = 100

export function AllocationsPage() {
  const [page, setPage] = useState(1)
  const [traceId, setTraceId] = useState('')

  const params = useMemo(
    () => ({ limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE, trace_id: traceId.trim() || undefined }),
    [page, traceId],
  )
  const query = useQuery({
    queryKey: qk.allocations.list(params),
    queryFn: ({ signal }) => allocationsApi.list(params, signal),
  })

  const columns: ColumnDef<AllocationRow, unknown>[] = [
    { header: 'when', cell: ({ row }) => formatIso(row.original.created_at, 'MM-DD HH:mm:ss') },
    { header: 'market', accessorKey: 'market_slug' },
    {
      header: 'target / actual',
      cell: ({ row }) => `${formatUsdc(row.original.target_budget_usdc)} → ${formatUsdc(row.original.buy_budget_usdc)}`,
    },
    {
      header: 'reason',
      cell: ({ row }) => <code style={{ fontSize: 11 }}>{row.original.reason ?? '—'}</code>,
    },
    {
      header: 'release',
      cell: ({ row }) => (
        <code style={{ fontSize: 11, color: 'var(--color-text-dim)' }}>{row.original.release_reason ?? '—'}</code>
      ),
    },
    { header: 'trace', cell: ({ row }) => <CopyableId value={row.original.trace_id ?? ''} head={4} tail={4} /> },
  ]

  const total = query.data?.total ?? query.data?.items?.length ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <>
      <PageHeader title="资金分配 Allocations" subtitle="reason / release_reason 用于审计资金调度" />
      <Group gap="xs" mb="sm">
        <TextInput size="xs" placeholder="trace_id" value={traceId} onChange={(e) => setTraceId(e.currentTarget.value)} w={300} />
      </Group>
      <DataTable<AllocationRow>
        columns={columns}
        data={query.data?.items}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        pagination={{ page, pageCount: totalPages, onChange: setPage }}
        rowKey={(a) => a.allocation_id}
      />
    </>
  )
}
