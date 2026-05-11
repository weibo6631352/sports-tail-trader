import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Tabs, Group, TextInput, NumberInput } from '@mantine/core'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { outboxApi } from '@core/api/resources'
import type { OutboxFailureRow, OutboxPendingRow } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { CopyableId } from '@shared/ui/CopyableId'
import { DataTable } from '@shared/tables/DataTable'
import { formatIso } from '@shared/format'

const PAGE_SIZE = 100

export function OutboxPage() {
  return (
    <>
      <PageHeader title="Outbox" subtitle="pending / failures · 持久化链路诊断" />
      <Tabs defaultValue="pending">
        <Tabs.List>
          <Tabs.Tab value="pending">Pending</Tabs.Tab>
          <Tabs.Tab value="failures">Failures</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="pending" pt="sm">
          <PendingTab />
        </Tabs.Panel>
        <Tabs.Panel value="failures" pt="sm">
          <FailuresTab />
        </Tabs.Panel>
      </Tabs>
    </>
  )
}

function PendingTab() {
  const [page, setPage] = useState(1)
  const [traceId, setTraceId] = useState('')
  const params = useMemo(
    () => ({ limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE, trace_id: traceId.trim() || undefined }),
    [page, traceId],
  )
  const query = useQuery({ queryKey: qk.outbox.pending(params), queryFn: ({ signal }) => outboxApi.pending(params, signal) })

  const columns: ColumnDef<OutboxPendingRow, unknown>[] = [
    { header: 'when', cell: ({ row }) => formatIso(row.original.created_at, 'MM-DD HH:mm:ss') },
    { header: 'event_type', accessorKey: 'event_type' },
    { header: 'retries', accessorKey: 'retry_count' },
    { header: 'trace', cell: ({ row }) => <CopyableId value={row.original.trace_id ?? ''} head={4} tail={4} /> },
  ]
  const total = query.data?.total ?? query.data?.items?.length ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <>
      <Group gap="xs" mb="sm">
        <TextInput
          size="xs"
          placeholder="trace_id"
          value={traceId}
          onChange={(e) => setTraceId(e.currentTarget.value)}
          w={300}
        />
      </Group>
      <DataTable<OutboxPendingRow>
        columns={columns}
        data={query.data?.items}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        pagination={{ page, pageCount: totalPages, onChange: setPage }}
        rowKey={(r) => r.event_id}
      />
    </>
  )
}

function FailuresTab() {
  const [page, setPage] = useState(1)
  const [eventType, setEventType] = useState('')
  const [minRetry, setMinRetry] = useState(1)
  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      event_type: eventType.trim() || undefined,
      min_retry_count: minRetry,
    }),
    [page, eventType, minRetry],
  )
  const query = useQuery({ queryKey: qk.outbox.failures(params), queryFn: ({ signal }) => outboxApi.failures(params, signal) })

  const columns: ColumnDef<OutboxFailureRow, unknown>[] = [
    {
      header: 'when',
      cell: ({ row }) => formatIso(row.original.updated_at ?? row.original.created_at, 'MM-DD HH:mm:ss'),
    },
    { header: 'event_type', accessorKey: 'event_type' },
    { header: 'retries', accessorKey: 'retry_count' },
    {
      header: 'last error',
      cell: ({ row }) => (
        <code style={{ fontSize: 11, color: 'var(--color-danger)' }}>{row.original.last_error ?? '—'}</code>
      ),
    },
    { header: 'trace', cell: ({ row }) => <CopyableId value={row.original.trace_id ?? ''} head={4} tail={4} /> },
  ]
  const total = query.data?.total ?? query.data?.items?.length ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <>
      <Group gap="xs" mb="sm" wrap="wrap">
        <TextInput
          size="xs"
          placeholder="event_type"
          value={eventType}
          onChange={(e) => setEventType(e.currentTarget.value)}
          w={240}
        />
        <NumberInput
          size="xs"
          label="min_retry_count"
          value={minRetry}
          onChange={(v) => setMinRetry(typeof v === 'number' ? v : 1)}
          min={0}
          max={100}
          w={150}
        />
      </Group>
      <DataTable<OutboxFailureRow>
        columns={columns}
        data={query.data?.items}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        pagination={{ page, pageCount: totalPages, onChange: setPage }}
        rowKey={(r) => r.event_id}
      />
    </>
  )
}
