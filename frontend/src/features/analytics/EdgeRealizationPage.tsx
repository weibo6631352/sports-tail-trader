import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, Stack, Text, TextInput } from '@mantine/core'
import {
  CartesianGrid,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from 'recharts'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { analyticsApi } from '@core/api/resources'
import type { EdgeRealizationBucket, EdgeRealizationItem } from '@core/api/types'
import { chartTooltipStyle } from '@shared/charts'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { DataTable } from '@shared/tables/DataTable'
import { CopyableId } from '@shared/ui/CopyableId'
import { TimeWindowPicker } from '@shared/time/TimeWindowPicker'
import { formatDecimal, toDecimal } from '@shared/format'
import { useTimeWindowStore } from '@core/time/store'
import { useAnalyticsFiltersStore } from '@core/filters/store'

export function EdgeRealizationPage() {
  const since = useTimeWindowStore((s) => s.since)
  const until = useTimeWindowStore((s) => s.until)
  const strategyId = useAnalyticsFiltersStore((s) => s.strategyId)
  const [conditionId, setConditionId] = useState('')
  const [submitted, setSubmitted] = useState<{ since?: number; until?: number; strategy_id?: string; condition_id?: string; limit?: number } | null>(null)

  const query = useQuery({
    queryKey: submitted ? qk.analytics.edgeRealization(submitted) : ['analytics', 'edge', 'idle'],
    queryFn: ({ signal }) => analyticsApi.edgeRealization(submitted ?? {}, signal),
    enabled: Boolean(submitted),
  })

  const items = query.data?.items ?? []
  const buckets = query.data?.buckets ?? []

  const scatterData = items
    .map((it) => {
      const x = toDecimal(it.predicted_edge_bps)?.toNumber()
      const y = toDecimal(it.actual_return_bps)?.toNumber()
      if (x === undefined || y === undefined) return null
      return { x, y, condition_id: it.condition_id, record_id: it.record_id }
    })
    .filter((p): p is { x: number; y: number; condition_id: string; record_id: string } => p !== null)

  const itemColumns: ColumnDef<EdgeRealizationItem, unknown>[] = [
    {
      header: 'record / condition',
      cell: ({ row }) => (
        <Stack gap={2}>
          <CopyableId value={row.original.record_id} dense />
          <CopyableId value={row.original.condition_id} dense />
        </Stack>
      ),
    },
    {
      header: 'predicted edge',
      cell: ({ row }) => formatDecimal(row.original.predicted_edge_bps, { dp: 1, suffix: ' bps' }),
    },
    {
      header: 'actual return',
      cell: ({ row }) => formatDecimal(row.original.actual_return_bps, { dp: 1, suffix: ' bps' }),
    },
    {
      header: 'cost',
      cell: ({ row }) => formatDecimal(row.original.cost_usdc, { dp: 2 }),
    },
    { header: 'status', accessorKey: 'position_status' },
  ]

  const bucketColumns: ColumnDef<EdgeRealizationBucket, unknown>[] = [
    { header: 'bucket', accessorKey: 'bucket' },
    { header: 'n', accessorKey: 'count' },
    { header: 'n_with_return', accessorKey: 'with_return_count' },
    {
      header: 'mean actual bps',
      cell: ({ row }) => formatDecimal(row.original.mean_actual_return_bps, { dp: 1 }),
    },
    {
      header: 'median bps',
      cell: ({ row }) => formatDecimal(row.original.median_actual_return_bps, { dp: 1 }),
    },
    {
      header: 'win rate',
      cell: ({ row }) => {
        const v = toDecimal(row.original.win_rate)?.mul(100).toFixed(1)
        return v ? `${v}%` : '—'
      },
    },
  ]

  return (
    <>
      <PageHeader
        title="Edge Realization"
        subtitle="预测 edge × 实际 per-share PnL · 按预测 edge 分桶给均值 / 中位数 / 胜率"
      />

      <Group justify="space-between" mb="md" wrap="wrap">
        <Group gap="sm" wrap="wrap">
          <TimeWindowPicker />
          <TextInput
            size="xs"
            placeholder="condition_id (可选)"
            value={conditionId}
            onChange={(e) => setConditionId(e.currentTarget.value)}
            w={320}
          />
        </Group>
        <InlineActionButton
          variant="accent"
          onClick={() =>
            setSubmitted({
              since: since ?? undefined,
              until: until ?? undefined,
              strategy_id: strategyId ?? undefined,
              condition_id: conditionId.trim() || undefined,
              limit: 500,
            })
          }
        >
          查询
        </InlineActionButton>
      </Group>

      {!submitted ? (
        <EmptyState title="点击查询" />
      ) : query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : (
        <Stack gap="md">
          <SectionCard title={`散点：预测 edge (x) × 实际 return (y)  ·  n=${scatterData.length}`}>
            <div style={{ width: '100%', height: 320 }}>
              <ResponsiveContainer>
                <ScatterChart>
                  <CartesianGrid strokeDasharray="3 3" stroke="#243352" />
                  <XAxis
                    type="number"
                    dataKey="x"
                    name="predicted edge"
                    unit=" bps"
                    stroke="#97a6c2"
                    tick={{ fontSize: 11 }}
                  />
                  <YAxis
                    type="number"
                    dataKey="y"
                    name="actual return"
                    unit=" bps"
                    stroke="#97a6c2"
                    tick={{ fontSize: 11 }}
                  />
                  <ZAxis range={[40, 80]} />
                  <Tooltip
                    cursor={{ strokeDasharray: '3 3' }}
                    contentStyle={chartTooltipStyle}
                  />
                  <Scatter data={scatterData} fill="#5cd9c5" />
                </ScatterChart>
              </ResponsiveContainer>
            </div>
            <Text size="xs" c="dimmed" mt="xs">
              y &gt; 0 表示该决策事后兑现了预测的 edge；y 远低于 x 表示模型可能高估了 edge。
            </Text>
          </SectionCard>

          <SectionCard title="桶聚合">
            <DataTable<EdgeRealizationBucket>
              columns={bucketColumns}
              data={buckets}
              rowKey={(r) => r.bucket}
            />
          </SectionCard>

          <SectionCard title={`明细 (limit=${query.data?.limit})`}>
            <DataTable<EdgeRealizationItem>
              columns={itemColumns}
              data={items}
              rowKey={(r) => r.record_id}
            />
          </SectionCard>
        </Stack>
      )}
    </>
  )
}
