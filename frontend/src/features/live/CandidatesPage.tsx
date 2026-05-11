import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Group, Select, TextInput } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import type { ColumnDef } from '@tanstack/react-table'
import { qk, qkRoots } from '@core/api/keys'
import { candidatesApi } from '@core/api/resources'
import type { Candidate } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { StatusPill } from '@shared/ui/StatusPill'
import { CopyableId } from '@shared/ui/CopyableId'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { DataTable } from '@shared/tables/DataTable'
import { confirmAction } from '@shared/forms/confirmAction'
import { describeError } from '@core/api/errors'

const PAGE_SIZE = 100

type Filters = {
  market_type?: string
  league?: string
  action?: string
  execution_permission?: string
  accepted?: 'true' | 'false' | ''
}

export function CandidatesPage() {
  const client = useQueryClient()
  const [page, setPage] = useState(1)
  const [filters, setFilters] = useState<Filters>({})

  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      market_type: filters.market_type || undefined,
      league: filters.league || undefined,
      action: filters.action || undefined,
      execution_permission: filters.execution_permission || undefined,
      accepted: filters.accepted ? filters.accepted === 'true' : undefined,
    }),
    [page, filters],
  )

  const query = useQuery({
    queryKey: qk.candidates.list(params),
    queryFn: ({ signal }) => candidatesApi.list(params, signal),
  })

  const confirmMutation = useMutation({
    mutationFn: candidatesApi.confirm,
    onSuccess: () => {
      notifications.show({ title: '候选已确认', message: '请求已提交，等待后端审计回看', color: 'teal' })
      client.invalidateQueries({ queryKey: qkRoots.candidates })
      client.invalidateQueries({ queryKey: qkRoots.auditEvents })
    },
    onError: (err) => {
      notifications.show({ title: '确认失败', message: describeError(err), color: 'red' })
    },
  })

  const columns: ColumnDef<Candidate, unknown>[] = useMemo(
    () => [
      {
        header: 'condition / token',
        cell: ({ row }) => (
          <div>
            <CopyableId value={row.original.condition_id} />
            {row.original.token_id ? (
              <div style={{ marginTop: 2 }}>
                <CopyableId value={row.original.token_id} label="tok" head={4} tail={4} />
              </div>
            ) : null}
          </div>
        ),
      },
      { header: 'market', accessorKey: 'market_slug' },
      { header: 'league', accessorKey: 'league' },
      { header: 'market_type', accessorKey: 'market_type' },
      {
        header: 'action',
        cell: ({ row }) => (
          <StatusPill tone={row.original.action === 'enter' ? 'accent' : 'neutral'} size="xs">
            {row.original.action ?? '—'}
          </StatusPill>
        ),
      },
      {
        header: 'execution',
        cell: ({ row }) => (
          <StatusPill
            tone={
              row.original.execution_permission === 'auto'
                ? 'success'
                : row.original.execution_permission === 'manual'
                  ? 'warning'
                  : 'neutral'
            }
            size="xs"
          >
            {row.original.execution_permission ?? '—'}
          </StatusPill>
        ),
      },
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
        cell: ({ row }) =>
          row.original.reason ? <code style={{ fontSize: 11 }}>{row.original.reason}</code> : '—',
      },
      {
        header: '操作',
        cell: ({ row }) => {
          const c = row.original
          if (!c.confirmable || !c.token_id) return <span style={{ color: 'var(--color-text-dim)' }}>—</span>
          return (
            <InlineActionButton
              variant="link"
              onClick={(e) => {
                e.stopPropagation()
                confirmAction({
                  title: `确认候选 ${c.market_slug ?? ''}`,
                  description: `将向后端提交手动确认。market_slug=${c.market_slug ?? '—'} · token_id=${c.token_id}`,
                  tone: 'warning',
                  defaultReason: 'manual_confirm_from_admin',
                  onConfirm: async ({ operator, reason, trace_id }) => {
                    await confirmMutation.mutateAsync({
                      token_id: c.token_id!,
                      condition_id: c.condition_id,
                      market_slug: c.market_slug ?? undefined,
                      operator,
                      note: reason,
                      trace_id,
                    })
                  },
                })
              }}
            >
              确认
            </InlineActionButton>
          )
        },
      },
    ],
    [confirmMutation],
  )

  const totalPages = Math.max(1, Math.ceil((query.data?.total ?? query.data?.items?.length ?? 0) / PAGE_SIZE))

  return (
    <>
      <PageHeader
        title="候选 Candidates"
        subtitle="SSE 推；按 league / market_type / execution_permission / accepted 过滤"
      />

      <Group gap="xs" mb="sm" wrap="wrap">
        <TextInput
          size="xs"
          placeholder="league"
          value={filters.league ?? ''}
          onChange={(e) => setFilters((f) => ({ ...f, league: e.currentTarget.value }))}
          w={120}
        />
        <TextInput
          size="xs"
          placeholder="market_type"
          value={filters.market_type ?? ''}
          onChange={(e) => setFilters((f) => ({ ...f, market_type: e.currentTarget.value }))}
          w={150}
        />
        <Select
          size="xs"
          placeholder="action"
          value={filters.action ?? null}
          data={['enter', 'exit', 'recovery', 'follow_up']}
          onChange={(value) => setFilters((f) => ({ ...f, action: value ?? undefined }))}
          clearable
          w={140}
        />
        <Select
          size="xs"
          placeholder="execution"
          value={filters.execution_permission ?? null}
          data={['auto', 'manual', 'record_only']}
          onChange={(value) => setFilters((f) => ({ ...f, execution_permission: value ?? undefined }))}
          clearable
          w={140}
        />
        <Select
          size="xs"
          placeholder="accepted"
          value={filters.accepted ?? null}
          data={[
            { value: 'true', label: 'accepted' },
            { value: 'false', label: 'rejected' },
          ]}
          onChange={(value) => setFilters((f) => ({ ...f, accepted: (value as 'true' | 'false' | null) ?? '' }))}
          clearable
          w={120}
        />
      </Group>

      <DataTable<Candidate>
        columns={columns}
        data={query.data?.items}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        pagination={{
          page,
          pageCount: totalPages,
          onChange: setPage,
        }}
        rowKey={(c) => `${c.condition_id}_${c.token_id ?? 'none'}`}
      />
    </>
  )
}
