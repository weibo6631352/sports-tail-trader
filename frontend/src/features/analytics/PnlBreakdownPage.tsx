import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, Select, Text, SimpleGrid, Stack } from '@mantine/core'
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
import { portfolioApi } from '@core/api/resources'
import type { PnlBreakdownRow, PnlBreakdownGroupBy } from '@core/api/types'
import { chartTooltipStyle } from '@shared/charts'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { DataTable } from '@shared/tables/DataTable'
import { formatUsdc, pnlTone, pnlToneColor, toDecimal } from '@shared/format'

const GROUP_OPTIONS: { value: PnlBreakdownGroupBy; label: string }[] = [
  { value: 'strategy_id', label: 'strategy_id' },
  { value: 'market_slug', label: 'market_slug' },
  { value: 'category', label: 'category' },
  { value: 'outcome', label: 'outcome' },
  { value: 'condition_id', label: 'condition_id' },
  { value: 'redeemable_status', label: 'redeemable_status' },
]

export function PnlBreakdownPage() {
  const [groupBy, setGroupBy] = useState<PnlBreakdownGroupBy>('strategy_id')
  const [submitted, setSubmitted] = useState<{ group_by: PnlBreakdownGroupBy } | null>(null)

  const query = useQuery({
    queryKey: submitted ? qk.portfolio.pnlBreakdown(submitted) : ['portfolio', 'pnl', 'idle'],
    queryFn: ({ signal }) => portfolioApi.pnlBreakdown(submitted!, signal),
    enabled: Boolean(submitted),
  })

  const columns: ColumnDef<PnlBreakdownRow, unknown>[] = [
    { header: 'group', accessorKey: 'group_key' },
    { header: 'positions', accessorKey: 'position_count' },
    {
      header: 'realized PnL',
      cell: ({ row }) => (
        <span style={{ color: pnlToneColor(pnlTone(row.original.realized_pnl_usdc)) }}>
          {formatUsdc(row.original.realized_pnl_usdc)}
        </span>
      ),
    },
    {
      header: 'cash PnL',
      cell: ({ row }) => (
        <span style={{ color: pnlToneColor(pnlTone(row.original.cash_pnl_usdc)) }}>
          {formatUsdc(row.original.cash_pnl_usdc)}
        </span>
      ),
    },
    { header: 'current value', cell: ({ row }) => formatUsdc(row.original.current_value_usdc) },
    { header: 'cost', cell: ({ row }) => formatUsdc(row.original.cost_usdc) },
  ]

  const chartData = (query.data?.rows ?? []).map((row) => ({
    group_key: row.group_key,
    realized: toDecimal(row.realized_pnl_usdc)?.toNumber() ?? 0,
    cash: toDecimal(row.cash_pnl_usdc)?.toNumber() ?? 0,
  }))

  return (
    <>
      <PageHeader title="PnL 分解" subtitle="按 group_by 维度聚合仓位 realized / cash PnL" />

      <Group justify="space-between" mb="md">
        <Group gap="sm">
          <Select
            size="xs"
            label="group_by"
            value={groupBy}
            data={GROUP_OPTIONS}
            onChange={(v) => v && setGroupBy(v as PnlBreakdownGroupBy)}
            w={200}
          />
        </Group>
        <InlineActionButton variant="accent" onClick={() => setSubmitted({ group_by: groupBy })}>
          查询
        </InlineActionButton>
      </Group>

      {!submitted ? (
        <EmptyState title="点击查询" />
      ) : query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : (
        <Stack gap="md">
          {query.data?.totals && (
            <SimpleGrid cols={{ base: 2, md: 4 }} spacing="md">
              <Total label="positions" value={String(query.data.totals.position_count)} />
              <Total
                label="realized PnL"
                value={formatUsdc(query.data.totals.realized_pnl_usdc)}
                tone={pnlTone(query.data.totals.realized_pnl_usdc)}
              />
              <Total
                label="cash PnL"
                value={formatUsdc(query.data.totals.cash_pnl_usdc)}
                tone={pnlTone(query.data.totals.cash_pnl_usdc)}
              />
              <Total label="current value" value={formatUsdc(query.data.totals.current_value_usdc)} />
            </SimpleGrid>
          )}

          <SectionCard title="柱状图 (realized vs cash)">
            <div style={{ width: '100%', height: 300 }}>
              <ResponsiveContainer>
                <BarChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#243352" />
                  <XAxis dataKey="group_key" stroke="#97a6c2" tick={{ fontSize: 10 }} angle={-15} textAnchor="end" height={50} />
                  <YAxis stroke="#97a6c2" tick={{ fontSize: 11 }} />
                  <Tooltip contentStyle={chartTooltipStyle} />
                  <Bar dataKey="realized" fill="#5cd9c5" name="realized" />
                  <Bar dataKey="cash" fill="#5aa9ff" name="cash" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </SectionCard>

          <SectionCard title="明细">
            <DataTable<PnlBreakdownRow>
              columns={columns}
              data={query.data?.rows}
              rowKey={(r) => r.group_key}
            />
          </SectionCard>
        </Stack>
      )}
    </>
  )
}

function Total({ label, value, tone }: { label: string; value: string; tone?: 'pos' | 'neg' | 'neutral' }) {
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
      </Stack>
    </SectionCard>
  )
}

