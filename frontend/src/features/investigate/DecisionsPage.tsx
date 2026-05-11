import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, Select, TextInput } from '@mantine/core'
import { useNavigate, useSearchParams } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { decisionsApi } from '@core/api/resources'
import type { DecisionRecord } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { CopyableId } from '@shared/ui/CopyableId'
import { StatusPill } from '@shared/ui/StatusPill'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { DataTable } from '@shared/tables/DataTable'
import { formatIso } from '@shared/format'
import { DecisionDetailDrawer } from './DecisionDetailDrawer'

const PAGE_SIZE = 100

export function DecisionsPage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [page, setPage] = useState(1)
  const [traceId, setTraceId] = useState(searchParams.get('trace_id') ?? '')
  const [conditionId, setConditionId] = useState(searchParams.get('condition_id') ?? '')
  const [accepted, setAccepted] = useState<'true' | 'false' | ''>('')
  const [drawerId, setDrawerId] = useState<string | null>(null)

  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      trace_id: traceId.trim() || undefined,
      condition_id: conditionId.trim() || undefined,
      accepted: accepted ? accepted === 'true' : undefined,
    }),
    [page, traceId, conditionId, accepted],
  )

  const query = useQuery({
    queryKey: qk.decisions.list(params),
    queryFn: ({ signal }) => decisionsApi.list(params, signal),
  })

  const columns: ColumnDef<DecisionRecord, unknown>[] = useMemo(
    () => [
      {
        header: 'when',
        cell: ({ row }) => (
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11 }}>
            {formatIso(row.original.created_at, 'MM-DD HH:mm:ss')}
          </span>
        ),
      },
      {
        header: 'hook',
        cell: ({ row }) => <code style={{ fontSize: 11 }}>{row.original.hook_name ?? '—'}</code>,
      },
      {
        header: 'condition / token',
        cell: ({ row }) => (
          <div>
            <CopyableId value={row.original.condition_id} dense />
            {row.original.token_id ? (
              <div style={{ marginTop: 2 }}>
                <CopyableId value={row.original.token_id} dense label="tok" />
              </div>
            ) : null}
          </div>
        ),
      },
      { header: 'market', accessorKey: 'market_slug' },
      {
        header: 'accepted',
        cell: ({ row }) => (
          <StatusPill tone={row.original.accepted ? 'success' : 'danger'} size="xs">
            {row.original.accepted ? 'yes' : 'no'}
          </StatusPill>
        ),
      },
      {
        header: 'reason',
        cell: ({ row }) => (
          <code style={{ fontSize: 11, color: 'var(--color-text-dim)' }}>{row.original.reason ?? '—'}</code>
        ),
      },
      {
        header: 'trace',
        cell: ({ row }) => <CopyableId value={row.original.trace_id} dense />,
      },
      {
        header: '操作',
        cell: ({ row }) => (
          <Group gap={6}>
            <InlineActionButton
              variant="link"
              onClick={(e) => {
                e.stopPropagation()
                setDrawerId(row.original.record_id)
              }}
            >
              详情
            </InlineActionButton>
            <InlineActionButton
              variant="link"
              onClick={(e) => {
                e.stopPropagation()
                navigate(`/investigate/timeline?condition_id=${encodeURIComponent(row.original.condition_id)}`)
              }}
            >
              timeline
            </InlineActionButton>
          </Group>
        ),
      },
    ],
    [navigate],
  )

  const total = query.data?.total ?? query.data?.items?.length ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <>
      <PageHeader
        title="决策记录 Decisions"
        subtitle="/admin/decisions/dump 列表 · 点详情打开完整 decision_input / decision_output JSONB"
      />
      <Group gap="xs" mb="sm" wrap="wrap">
        <TextInput
          size="xs"
          placeholder="trace_id"
          value={traceId}
          onChange={(e) => setTraceId(e.currentTarget.value)}
          w={300}
        />
        <TextInput
          size="xs"
          placeholder="condition_id (0x...)"
          value={conditionId}
          onChange={(e) => setConditionId(e.currentTarget.value)}
          w={320}
        />
        <Select
          size="xs"
          placeholder="accepted"
          value={accepted || null}
          data={[
            { value: 'true', label: 'accepted' },
            { value: 'false', label: 'rejected' },
          ]}
          onChange={(v) => setAccepted((v as 'true' | 'false' | null) ?? '')}
          clearable
          w={150}
        />
      </Group>

      <DataTable<DecisionRecord>
        columns={columns}
        data={query.data?.items}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        pagination={{ page, pageCount: totalPages, onChange: setPage }}
        rowKey={(r) => r.record_id}
        onRowClick={(row) => setDrawerId(row.original.record_id)}
      />

      <DecisionDetailDrawer recordId={drawerId} onClose={() => setDrawerId(null)} />
    </>
  )
}
