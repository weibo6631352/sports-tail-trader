import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, NumberInput, Text } from '@mantine/core'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  Legend,
} from 'recharts'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { healthApi } from '@core/api/resources'
import type { LatencyPercentile } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { DataTable } from '@shared/tables/DataTable'

export function LatencyPage() {
  const [sampleLimit, setSampleLimit] = useState(500)
  const [submitted, setSubmitted] = useState<{ sample_limit: number } | null>(null)

  const query = useQuery({
    queryKey: submitted ? qk.latency(submitted) : ['latency', 'idle'],
    queryFn: ({ signal }) => healthApi.latencyPercentiles(submitted!, signal),
    enabled: Boolean(submitted),
  })

  const columns: ColumnDef<LatencyPercentile, unknown>[] = [
    { header: 'stage', accessorKey: 'stage' },
    { header: 'n', accessorKey: 'sample_count' },
    { header: 'p50', accessorKey: 'p50_ms', cell: ({ row }) => fmt(row.original.p50_ms) },
    { header: 'p90', accessorKey: 'p90_ms', cell: ({ row }) => fmt(row.original.p90_ms) },
    { header: 'p95', accessorKey: 'p95_ms', cell: ({ row }) => fmt(row.original.p95_ms) },
    { header: 'p99', accessorKey: 'p99_ms', cell: ({ row }) => fmt(row.original.p99_ms) },
    { header: 'min', cell: ({ row }) => fmt(row.original.min_ms) },
    { header: 'max', cell: ({ row }) => fmt(row.original.max_ms) },
  ]

  return (
    <>
      <PageHeader
        title="执行延迟分位 Latency Percentiles"
        subtitle="stage = queue→sign / sign→submit / submit→ack / queue→ack"
      />
      <Group justify="space-between" mb="md">
        <NumberInput
          size="xs"
          label="sample_limit"
          value={sampleLimit}
          onChange={(v) => setSampleLimit(typeof v === 'number' ? v : 500)}
          min={1}
          max={5000}
          step={100}
          w={150}
        />
        <InlineActionButton
          variant="accent"
          onClick={() => setSubmitted({ sample_limit: sampleLimit })}
        >
          查询
        </InlineActionButton>
      </Group>

      {!submitted ? (
        <EmptyState title="点击查询" />
      ) : query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : (
        <>
          <SectionCard title="分位柱状图 (ms)">
            <div style={{ width: '100%', height: 280 }}>
              <ResponsiveContainer>
                <BarChart data={query.data?.stages ?? []}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#243352" />
                  <XAxis dataKey="stage" stroke="#97a6c2" tick={{ fontSize: 10 }} angle={-10} textAnchor="end" height={50} />
                  <YAxis stroke="#97a6c2" tick={{ fontSize: 11 }} />
                  <Tooltip contentStyle={{ background: '#121e36', border: '1px solid #243352' }} />
                  <Legend />
                  <Bar dataKey="p50_ms" fill="#5cd9c5" name="p50" />
                  <Bar dataKey="p95_ms" fill="#f0b955" name="p95" />
                  <Bar dataKey="p99_ms" fill="#f06568" name="p99" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </SectionCard>
          <SectionCard title="明细">
            <DataTable<LatencyPercentile>
              columns={columns}
              data={query.data?.stages}
              rowKey={(r) => r.stage}
            />
            <Text size="xs" c="dimmed" mt="xs">
              数据来自 outbox_events.payload.timestamps，从最近 {query.data?.sample_limit ?? sampleLimit} 条 order
              lifecycle 事件抽样计算。
            </Text>
          </SectionCard>
        </>
      )}
    </>
  )
}

function fmt(v?: number): string {
  if (v === undefined || !Number.isFinite(v)) return '—'
  return v.toFixed(0)
}
