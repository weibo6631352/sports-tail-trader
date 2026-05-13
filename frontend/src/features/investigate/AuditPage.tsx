import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, Stack, Tabs, Text, TextInput, NumberInput } from '@mantine/core'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { auditEventsApi } from '@core/api/resources'
import type {
  AuditEventRow,
  OperatorAggregateEvent,
} from '@core/api/types'
import { chartTooltipStyle } from '@shared/charts'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { CopyableId } from '@shared/ui/CopyableId'
import { StatusPill } from '@shared/ui/StatusPill'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { MonoCell, DimMonoCell } from '@shared/ui/MonoCell'
import { DataTable } from '@shared/tables/DataTable'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { TimeWindowPicker } from '@shared/time/TimeWindowPicker'
import { formatIso } from '@shared/format'
import { useTimeWindowStore } from '@core/time/store'

const PAGE_SIZE = 100

export function AuditPage() {
  return (
    <>
      <PageHeader
        title="审计 Audit"
        subtitle="audit_events 明细 + 按 operator 聚合（谁动了什么）"
      />
      <Tabs defaultValue="list">
        <Tabs.List>
          <Tabs.Tab value="list">明细</Tabs.Tab>
          <Tabs.Tab value="operators">按 operator 聚合</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="list" pt="sm">
          <ListTab />
        </Tabs.Panel>
        <Tabs.Panel value="operators" pt="sm">
          <OperatorTab />
        </Tabs.Panel>
      </Tabs>
    </>
  )
}

function ListTab() {
  const [page, setPage] = useState(1)
  const [traceId, setTraceId] = useState('')
  const [eventTitle, setEventTitle] = useState('')

  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      trace_id: traceId.trim() || undefined,
      event_title: eventTitle.trim() || undefined,
    }),
    [page, traceId, eventTitle],
  )

  const query = useQuery({ queryKey: qk.auditEvents.list(params), queryFn: ({ signal }) => auditEventsApi.list(params, signal) })

  const columns: ColumnDef<AuditEventRow, unknown>[] = useMemo(
    () => [
      { header: 'when', cell: ({ row }) => formatIso(row.original.created_at, 'MM-DD HH:mm:ss') },
      { header: 'title', cell: ({ row }) => <MonoCell>{row.original.event_title}</MonoCell> },
      {
        header: 'status',
        cell: ({ row }) =>
          row.original.status ? <StatusPill size="xs">{row.original.status}</StatusPill> : <span>—</span>,
      },
      { header: 'operator', accessorKey: 'operator' },
      {
        header: 'reason',
        cell: ({ row }) => (
          <DimMonoCell>{row.original.reason ?? '—'}</DimMonoCell>
        ),
      },
      {
        header: 'condition',
        cell: ({ row }) => <CopyableId value={row.original.condition_id ?? ''} dense />,
      },
      {
        header: 'trace',
        cell: ({ row }) => <CopyableId value={row.original.trace_id ?? ''} dense />,
      },
    ],
    [],
  )

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
        <TextInput
          size="xs"
          placeholder="event_title"
          value={eventTitle}
          onChange={(e) => setEventTitle(e.currentTarget.value)}
          w={260}
        />
      </Group>
      <DataTable<AuditEventRow>
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

function OperatorTab() {
  const since = useTimeWindowStore((s) => s.since)
  const until = useTimeWindowStore((s) => s.until)
  const [selectedOperator, setSelectedOperator] = useState<string | null>(null)
  const [sampleLimit, setSampleLimit] = useState(2000)
  const [submitted, setSubmitted] = useState<{ since?: number; until?: number; sample_limit: number } | null>(null)

  // 总览：operator=null → by_operator + by_event_title 全分布
  const overview = useQuery({
    queryKey: submitted
      ? qk.auditEvents.operators({ ...submitted, operator: null })
      : ['audit-events', 'operators', 'idle'],
    queryFn: ({ signal }) =>
      auditEventsApi.operators(
        {
          sample_limit: submitted?.sample_limit ?? 0,
          since: submitted?.since,
          until: submitted?.until,
        },
        signal,
      ),
    enabled: Boolean(submitted),
  })

  // 选中 operator：拉单 operator 详情
  const detail = useQuery({
    queryKey: submitted && selectedOperator
      ? qk.auditEvents.operators({ ...submitted, operator: selectedOperator })
      : ['audit-events', 'operators', 'detail-idle'],
    queryFn: ({ signal }) =>
      auditEventsApi.operators(
        {
          operator: selectedOperator ?? '',
          sample_limit: submitted?.sample_limit ?? 0,
          since: submitted?.since,
          until: submitted?.until,
        },
        signal,
      ),
    enabled: Boolean(submitted && selectedOperator),
  })

  const handleQuery = () => {
    setSubmitted({
      sample_limit: sampleLimit,
      since: since ?? undefined,
      until: until ?? undefined,
    })
    setSelectedOperator(null)
  }

  const eventColumns: ColumnDef<OperatorAggregateEvent, unknown>[] = [
    { header: 'when', cell: ({ row }) => formatIso(row.original.created_at, 'MM-DD HH:mm:ss') },
    { header: 'event_title', cell: ({ row }) => <MonoCell>{row.original.event_title}</MonoCell> },
    { header: 'operator', accessorKey: 'operator' },
    {
      header: 'reason',
      cell: ({ row }) => (
        <DimMonoCell>{row.original.reason ?? '—'}</DimMonoCell>
      ),
    },
    {
      header: 'condition',
      cell: ({ row }) => <CopyableId value={row.original.condition_id ?? ''} dense />,
    },
  ]

  return (
    <Stack gap="md">
      <Group justify="space-between" wrap="wrap" align="flex-end">
        <Group gap="sm" wrap="wrap" align="flex-end">
          <TimeWindowPicker />
          <NumberInput
            size="xs"
            label="sample_limit"
            value={sampleLimit}
            onChange={(v) => setSampleLimit(typeof v === 'number' ? v : 2000)}
            min={1}
            max={10_000}
            step={500}
            w={140}
          />
        </Group>
        <InlineActionButton variant="accent" onClick={handleQuery}>
          查询
        </InlineActionButton>
      </Group>

      {!submitted ? (
        <EmptyState title="点击查询" description="按 operator 聚合属于复盘视图——手动触发即可" />
      ) : overview.error ? (
        <QueryErrorNotice error={overview.error} onRetry={() => overview.refetch()} />
      ) : (
        <>
          <Group gap="md" wrap="wrap" align="flex-start">
            <SectionCard
              title="by_operator"
              description={`样本 ${overview.data?.total_events ?? 0} 条（点条选定 operator）`}
              style={{ flex: 1, minWidth: 360 }}
            >
              <HorizontalBar
                data={overview.data?.by_operator?.map((b) => ({ key: b.operator, count: b.count })) ?? []}
                onSelect={(key) => setSelectedOperator(key === selectedOperator ? null : key)}
                selected={selectedOperator}
                color="#5cd9c5"
              />
            </SectionCard>
            <SectionCard
              title="by_event_title"
              description={selectedOperator ? `当前过滤：operator=${selectedOperator}` : '总分布'}
              style={{ flex: 1, minWidth: 360 }}
            >
              <HorizontalBar
                data={
                  (selectedOperator ? detail.data : overview.data)?.by_event_title?.map((b) => ({
                    key: b.event_title,
                    count: b.count,
                  })) ?? []
                }
                color="#5aa9ff"
              />
            </SectionCard>
          </Group>

          {selectedOperator ? (
            <SectionCard
              title={`operator=${selectedOperator} 最近事件`}
              description={
                detail.data
                  ? `${detail.data.events.length} 条 / 总 ${detail.data.total_events}`
                  : '加载中…'
              }
              actions={
                <InlineActionButton variant="link" onClick={() => setSelectedOperator(null)}>
                  清除选择
                </InlineActionButton>
              }
            >
              {detail.error ? (
                <QueryErrorNotice error={detail.error} compact />
              ) : (
                <DataTable<OperatorAggregateEvent>
                  columns={eventColumns}
                  data={detail.data?.events}
                  isLoading={detail.isLoading}
                  rowKey={(e) => e.event_id}
                />
              )}
            </SectionCard>
          ) : (
            <Text size="xs" c="dimmed">
              点左侧某个 operator 条查看其最近事件。
            </Text>
          )}
        </>
      )}
    </Stack>
  )
}

function HorizontalBar({
  data,
  color,
  onSelect,
  selected,
}: {
  data: Array<{ key: string; count: number }>
  color: string
  onSelect?: (key: string) => void
  selected?: string | null
}) {
  if (data.length === 0) {
    return (
      <Text c="dimmed" size="sm">
        暂无数据
      </Text>
    )
  }
  return (
    <div style={{ width: '100%', height: Math.min(360, 30 + 28 * data.length) }}>
      <ResponsiveContainer>
        <BarChart data={data} layout="vertical" margin={{ left: 80 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#243352" />
          <XAxis type="number" stroke="#97a6c2" tick={{ fontSize: 11 }} />
          <YAxis type="category" dataKey="key" stroke="#97a6c2" tick={{ fontSize: 11 }} width={120} />
          <Tooltip contentStyle={chartTooltipStyle} />
          <Bar
            dataKey="count"
            fill={color}
            cursor={onSelect ? 'pointer' : undefined}
            onClick={(payload) => {
              if (!onSelect) return
              const key = (payload?.payload as { key?: string } | undefined)?.key
              if (key) onSelect(key)
            }}
            opacity={selected ? 0.85 : 1}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

