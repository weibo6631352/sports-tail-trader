import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, NumberInput, SimpleGrid, Stack, Text } from '@mantine/core'
import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from 'recharts'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { analyticsApi } from '@core/api/resources'
import type { CalibrationBucket } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { DataTable } from '@shared/tables/DataTable'
import { TimeWindowPicker } from '@shared/time/TimeWindowPicker'
import { useTimeWindowStore } from '@core/time/store'
import { useAnalyticsFiltersStore } from '@core/filters/store'
import { toDecimal } from '@shared/format'

// Calibration page: reliability diagram + Brier / log-loss.
// 后端 /analytics/calibration 返回:
//   buckets: [{lower, upper, prediction_count, outcome_count, mean_prediction, empirical_rate}]
//   brier_score: string | null  (mean((fair - outcome)^2))
//   log_loss:    string | null
//   total_samples / with_outcome_count

export function CalibrationPage() {
  const since = useTimeWindowStore((s) => s.since)
  const until = useTimeWindowStore((s) => s.until)
  const strategyId = useAnalyticsFiltersStore((s) => s.strategyId)
  const [bucketSize, setBucketSize] = useState(0.05)
  const [sampleLimit, setSampleLimit] = useState(2000)
  const [submitted, setSubmitted] = useState<{
    bucket_size: number
    strategy_id?: string
    since?: number
    until?: number
    sample_limit: number
  } | null>(null)

  const query = useQuery({
    queryKey: submitted ? qk.analytics.calibration(submitted) : ['analytics', 'calibration', 'idle'],
    queryFn: ({ signal }) => analyticsApi.calibration(submitted!, signal),
    enabled: Boolean(submitted),
  })

  const buckets = query.data?.buckets ?? []
  const chartData = buckets
    .filter((b) => b.empirical_rate !== null && b.mean_prediction !== null)
    .map((b) => {
      const x = toDecimal(b.mean_prediction)?.toNumber()
      const y = b.empirical_rate ? Number(b.empirical_rate) : null
      if (x === undefined || y === null || !Number.isFinite(y)) return null
      return { x, y, count: b.outcome_count }
    })
    .filter((p): p is { x: number; y: number; count: number } => p !== null)

  const columns: ColumnDef<CalibrationBucket, unknown>[] = [
    {
      header: 'bucket',
      cell: ({ row }) => `${row.original.lower} – ${row.original.upper}`,
    },
    { header: 'predictions', accessorKey: 'prediction_count' },
    { header: 'with outcome', accessorKey: 'outcome_count' },
    { header: 'mean prediction', accessorKey: 'mean_prediction' },
    {
      header: 'empirical rate',
      cell: ({ row }) =>
        row.original.empirical_rate
          ? `${(Number(row.original.empirical_rate) * 100).toFixed(1)}%`
          : '—',
    },
    {
      header: 'calibration error',
      cell: ({ row }) => {
        const pred = toDecimal(row.original.mean_prediction)?.toNumber()
        const emp = row.original.empirical_rate ? Number(row.original.empirical_rate) : null
        if (pred === undefined || emp === null) return '—'
        const diff = emp - pred
        const color = Math.abs(diff) > 0.1 ? 'var(--color-danger)' : Math.abs(diff) > 0.05 ? 'var(--color-warning)' : undefined
        return <span style={{ color }}>{diff >= 0 ? '+' : ''}{(diff * 100).toFixed(1)}pp</span>
      },
    },
  ]

  return (
    <>
      <PageHeader
        title="校准 Calibration"
        subtitle="reliability diagram · Brier score · log-loss — 判断 fair_value 是否被市场结算兑现"
      />
      <Group justify="space-between" mb="md" wrap="wrap">
        <Group gap="sm" wrap="wrap" align="flex-end">
          <TimeWindowPicker />
          <NumberInput
            size="xs"
            label="bucket_size"
            value={bucketSize}
            onChange={(v) => setBucketSize(typeof v === 'number' ? v : 0.05)}
            min={0.01}
            max={0.5}
            step={0.05}
            decimalScale={3}
            w={120}
          />
          <NumberInput
            size="xs"
            label="sample_limit"
            value={sampleLimit}
            onChange={(v) => setSampleLimit(typeof v === 'number' ? v : 2000)}
            min={1}
            max={10000}
            step={500}
            w={140}
          />
        </Group>
        <InlineActionButton
          variant="accent"
          onClick={() =>
            setSubmitted({
              bucket_size: bucketSize,
              strategy_id: strategyId ?? undefined,
              since: since ?? undefined,
              until: until ?? undefined,
              sample_limit: sampleLimit,
            })
          }
        >
          查询
        </InlineActionButton>
      </Group>

      {!submitted ? (
        <EmptyState
          title="点击查询"
          description="Calibration 是聚合分析；不会自动触发；样本要等市场结算后才有 outcome。"
        />
      ) : query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : (
        <Stack gap="md">
          <SimpleGrid cols={{ base: 2, md: 4 }} spacing="md">
            <Stat label="total samples" value={String(query.data?.total_samples ?? 0)} />
            <Stat
              label="with outcome"
              value={`${query.data?.with_outcome_count ?? 0}`}
              hint={`${pctOf(query.data?.with_outcome_count, query.data?.total_samples)}`}
            />
            <Stat
              label="Brier score"
              value={query.data?.brier_score ?? '—'}
              hint="0 = 完美；0.25 = 随机；≤ 0.10 = 良好"
              tone={brierTone(query.data?.brier_score)}
            />
            <Stat
              label="log-loss"
              value={query.data?.log_loss ?? '—'}
              hint="越小越好；与 Brier 互补检测尾部误差"
            />
          </SimpleGrid>

          <SectionCard title="Reliability diagram · 预测 vs 实际">
            <div style={{ width: '100%', height: 360 }}>
              <ResponsiveContainer>
                <ComposedChart data={chartData} margin={{ top: 16, right: 24, bottom: 24, left: 24 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#243352" />
                  <XAxis
                    type="number"
                    dataKey="x"
                    domain={[0, 1]}
                    stroke="#97a6c2"
                    tick={{ fontSize: 11 }}
                    label={{ value: 'mean predicted fair_value', position: 'bottom', fill: '#97a6c2', fontSize: 11 }}
                  />
                  <YAxis
                    type="number"
                    dataKey="y"
                    domain={[0, 1]}
                    stroke="#97a6c2"
                    tick={{ fontSize: 11 }}
                    label={{ value: 'empirical hit rate', angle: -90, position: 'insideLeft', fill: '#97a6c2', fontSize: 11 }}
                  />
                  <ZAxis dataKey="count" range={[40, 240]} />
                  <Tooltip
                    cursor={{ strokeDasharray: '3 3' }}
                    contentStyle={{ background: '#121e36', border: '1px solid #243352' }}
                    formatter={(value) => (typeof value === 'number' ? value.toFixed(3) : String(value))}
                  />
                  <ReferenceLine
                    segment={[
                      { x: 0, y: 0 },
                      { x: 1, y: 1 },
                    ]}
                    stroke="#f0b955"
                    strokeDasharray="6 4"
                    ifOverflow="extendDomain"
                  />
                  {/* 对角线视为完美校准；点偏离 = 模型高估/低估。 */}
                  <Line type="monotone" data={[{ x: 0, y: 0 }, { x: 1, y: 1 }]} dataKey="y" stroke="transparent" dot={false} legendType="none" />
                  <Scatter data={chartData} fill="#5cd9c5" name="bucket（点大小 = 样本数）" />
                  <Legend />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
            <Text size="xs" c="dimmed" mt="xs">
              点越接近对角线 = 预测与实际命中率一致。点位于对角线右下方 = 高估；左上方 = 低估。
            </Text>
          </SectionCard>

          <SectionCard title="桶明细">
            <DataTable<CalibrationBucket>
              columns={columns}
              data={buckets}
              isLoading={query.isLoading}
              rowKey={(b) => `${b.lower}_${b.upper}`}
            />
          </SectionCard>
        </Stack>
      )}
    </>
  )
}

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: string
  hint?: string
  tone?: 'pos' | 'neg' | 'neutral'
}) {
  const color = tone === 'pos' ? 'var(--color-success)' : tone === 'neg' ? 'var(--color-danger)' : undefined
  return (
    <SectionCard>
      <Stack gap={2}>
        <Text size="xs" c="dimmed">
          {label}
        </Text>
        <Text size="xl" fw={700} c={color}>
          {value}
        </Text>
        {hint ? (
          <Text size="xs" c="dimmed">
            {hint}
          </Text>
        ) : null}
      </Stack>
    </SectionCard>
  )
}

function brierTone(brier: string | null | undefined): 'pos' | 'neg' | 'neutral' | undefined {
  if (!brier) return undefined
  const v = Number(brier)
  if (!Number.isFinite(v)) return undefined
  if (v <= 0.1) return 'pos'
  if (v >= 0.2) return 'neg'
  return 'neutral'
}

function pctOf(part: number | undefined, total: number | undefined): string {
  if (!part || !total) return '—'
  return `${((part / total) * 100).toFixed(1)}% 已结算`
}

