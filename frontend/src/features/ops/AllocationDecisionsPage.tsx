import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, TextInput } from '@mantine/core'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { allocationsApi } from '@core/api/resources'
import type { AllocationDecisionEvent } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { DataTable } from '@shared/tables/DataTable'
import { CopyableId } from '@shared/ui/CopyableId'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { JsonPanel } from '@shared/ui/JsonPanel'
import { formatIso } from '@shared/format'

const PAGE_SIZE = 100

// AllocationPlan 决策过程——回答「为什么选这个市场不选那个」。
// 后端把过程落 audit_events.event_title='allocation_decision_recorded'，
// payload 含 candidates / selected_condition_ids / skipped_reasons / budget。

export function AllocationDecisionsPage() {
  const [page, setPage] = useState(1)
  const [conditionId, setConditionId] = useState('')
  const [openId, setOpenId] = useState<string | null>(null)

  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      condition_id: conditionId.trim() || undefined,
    }),
    [page, conditionId],
  )
  const query = useQuery({
    queryKey: qk.allocations.decisions(params),
    queryFn: ({ signal }) => allocationsApi.decisions(params, signal),
  })

  const columns: ColumnDef<AllocationDecisionEvent, unknown>[] = [
    { header: 'when', cell: ({ row }) => formatIso(row.original.created_at, 'MM-DD HH:mm:ss') },
    {
      header: 'condition',
      cell: ({ row }) => <CopyableId value={row.original.condition_id ?? ''} head={4} tail={4} />,
    },
    {
      header: '候选数',
      cell: ({ row }) => {
        const p = row.original.payload ?? {}
        const arr = Array.isArray(p.candidates) ? p.candidates : []
        return <span>{arr.length}</span>
      },
    },
    {
      header: '入选数',
      cell: ({ row }) => {
        const p = row.original.payload ?? {}
        const sel = Array.isArray(p.selected_condition_ids) ? p.selected_condition_ids : []
        return <span>{sel.length}</span>
      },
    },
    {
      header: 'budget',
      cell: ({ row }) => {
        const p = row.original.payload ?? {}
        if (p.budget === null || p.budget === undefined) return '—'
        if (typeof p.budget === 'string' || typeof p.budget === 'number') return String(p.budget)
        try {
          return <code style={{ fontSize: 11 }}>{JSON.stringify(p.budget).slice(0, 40)}</code>
        } catch {
          return '—'
        }
      },
    },
    {
      header: 'reason',
      cell: ({ row }) => (
        <code style={{ fontSize: 11, color: 'var(--color-text-dim)' }}>{row.original.reason ?? '—'}</code>
      ),
    },
    {
      header: 'trace',
      cell: ({ row }) => <CopyableId value={row.original.trace_id ?? ''} head={4} tail={4} />,
    },
    {
      header: '操作',
      cell: ({ row }) => (
        <InlineActionButton
          variant="link"
          onClick={(e) => {
            e.stopPropagation()
            setOpenId(openId === row.original.event_id ? null : row.original.event_id)
          }}
        >
          {openId === row.original.event_id ? '收起' : '展开 payload'}
        </InlineActionButton>
      ),
    },
  ]

  const total = query.data?.total ?? query.data?.items?.length ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const openEvent = query.data?.items.find((e) => e.event_id === openId)

  return (
    <>
      <PageHeader
        title="资金分配决策 Allocation Decisions"
        subtitle="audit_events.allocation_decision_recorded — candidates / selected / skipped_reasons"
      />
      <Group gap="xs" mb="sm">
        <TextInput
          size="xs"
          placeholder="condition_id (可选)"
          value={conditionId}
          onChange={(e) => setConditionId(e.currentTarget.value)}
          w={320}
        />
      </Group>

      <DataTable<AllocationDecisionEvent>
        columns={columns}
        data={query.data?.items}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        pagination={{ page, pageCount: totalPages, onChange: setPage }}
        rowKey={(r) => r.event_id}
      />

      {openEvent ? (
        <SectionCard
          title={`决策过程 ${openEvent.event_id.slice(0, 12)}…`}
          description={`${formatIso(openEvent.created_at)} · ${openEvent.condition_id ?? '—'}`}
          mt="md"
        >
          <JsonPanel value={openEvent.payload ?? {}} maxHeight={420} />
        </SectionCard>
      ) : null}
    </>
  )
}

