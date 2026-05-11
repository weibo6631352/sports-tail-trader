import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Checkbox, Group, TextInput } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import type { ColumnDef } from '@tanstack/react-table'
import { qk, qkRoots } from '@core/api/keys'
import { operationsApi } from '@core/api/resources'
import type { ReconcileDiffRow } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { CopyableId } from '@shared/ui/CopyableId'
import { StatusPill } from '@shared/ui/StatusPill'
import { MonoCell } from '@shared/ui/MonoCell'
import { DataTable } from '@shared/tables/DataTable'
import { confirmAction } from '@shared/forms/confirmAction'
import { formatDecimal, formatIso, formatUsdc } from '@shared/format'
import { describeError } from '@core/api/errors'

const PAGE_SIZE = 100

export function ReconcilePage() {
  const client = useQueryClient()
  const [page, setPage] = useState(1)
  const [conditionId, setConditionId] = useState('')
  const [includeStarted, setIncludeStarted] = useState(false)
  const [includeApplied, setIncludeApplied] = useState(true)

  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      condition_id: conditionId.trim() || undefined,
      include_started: includeStarted,
      include_applied: includeApplied,
    }),
    [page, conditionId, includeStarted, includeApplied],
  )

  const query = useQuery({
    queryKey: qk.operations.reconcileDiffs(params),
    queryFn: ({ signal }) => operationsApi.reconcileDiffs(params, signal),
  })

  const reconcile = useMutation({
    mutationFn: operationsApi.reconcile,
    onSuccess: () => {
      notifications.show({ title: 'reconcile 已触发', message: '结果以 outbox 事件为准', color: 'teal' })
      client.invalidateQueries({ queryKey: qkRoots.operations })
    },
    onError: (err) => notifications.show({ title: '失败', message: describeError(err), color: 'red' }),
  })

  const columns: ColumnDef<ReconcileDiffRow, unknown>[] = [
    { header: 'when', cell: ({ row }) => formatIso(row.original.created_at, 'MM-DD HH:mm:ss') },
    { header: 'action_type', accessorKey: 'action_type' },
    { header: 'market', accessorKey: 'market_slug' },
    {
      header: 'target size',
      cell: ({ row }) => formatDecimal(row.original.target_size_shares, { dp: 2 }),
    },
    {
      header: 'target notional',
      cell: ({ row }) => formatUsdc(row.original.target_notional_usdc),
    },
    {
      header: 'pause_reason',
      cell: ({ row }) =>
        row.original.pause_reason ? <MonoCell>{row.original.pause_reason}</MonoCell> : '—',
    },
    {
      header: 'status',
      cell: ({ row }) => <StatusPill size="xs">{row.original.status ?? '—'}</StatusPill>,
    },
    { header: 'trace', cell: ({ row }) => <CopyableId value={row.original.trace_id ?? ''} dense /> },
  ]

  const total = query.data?.total ?? query.data?.items?.length ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <>
      <PageHeader
        title="Reconcile"
        subtitle="diff 历史 · 全量 / 定向触发"
        actions={
          <Button
            size="xs"
            color="accent"
            onClick={() =>
              confirmAction({
                title: 'Reconcile 全量',
                description: '扫描所有 tracked 市场重新对齐；结果以 outbox 事件为准。',
                onConfirm: async ({ trace_id }) => reconcile.mutateAsync({ trace_id }),
              })
            }
          >
            触发全量 reconcile
          </Button>
        }
      />
      <SectionCard title="过滤">
        <Group gap="xs" wrap="wrap">
          <TextInput
            size="xs"
            placeholder="condition_id"
            value={conditionId}
            onChange={(e) => setConditionId(e.currentTarget.value)}
            w={320}
          />
          <Checkbox
            size="xs"
            label="include_started"
            checked={includeStarted}
            onChange={(e) => setIncludeStarted(e.currentTarget.checked)}
          />
          <Checkbox
            size="xs"
            label="include_applied"
            checked={includeApplied}
            onChange={(e) => setIncludeApplied(e.currentTarget.checked)}
          />
        </Group>
      </SectionCard>
      <DataTable<ReconcileDiffRow>
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
