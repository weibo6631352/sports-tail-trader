import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, NumberInput, SimpleGrid, Stack, Text } from '@mantine/core'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { analyticsApi } from '@core/api/resources'
import type { MissedOpportunityItem, MissedOpportunityReasonBucket } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { StatusPill } from '@shared/ui/StatusPill'
import { CopyableId } from '@shared/ui/CopyableId'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { DataTable } from '@shared/tables/DataTable'
import { TimeWindowPicker } from '@shared/time/TimeWindowPicker'
import { formatDecimal, formatIso, formatUsdc, pnlTone, pnlToneColor, toDecimal } from '@shared/format'
import { useTimeWindowStore } from '@core/time/store'
import { useAnalyticsFiltersStore } from '@core/filters/store'

// 对 accepted=false 的决策做事后盈利模拟，配合 /analytics/risk-rejections 用——
// 判断风控阈值是否过严。每笔模拟用 per_decision_usdc 等额入场。

const STATUS_TONE: Record<string, 'success' | 'danger' | 'neutral' | 'warning'> = {
  would_have_won: 'success',
  would_have_lost: 'danger',
  unsettled: 'neutral',
  unscorable: 'warning',
}

const STATUS_LABEL: Record<string, string> = {
  would_have_won: '本应赚',
  would_have_lost: '本应亏',
  unsettled: '未结算',
  unscorable: '无法评分',
}

export function MissedOpportunitiesPage() {
  const since = useTimeWindowStore((s) => s.since)
  const until = useTimeWindowStore((s) => s.until)
  const strategyId = useAnalyticsFiltersStore((s) => s.strategyId)
  const [perDecisionUsdc, setPerDecisionUsdc] = useState(10)
  const [limit, setLimit] = useState(500)
  const [submitted, setSubmitted] = useState<{
    limit: number
    per_decision_usdc: number
    strategy_id?: string
    since?: number
    until?: number
  } | null>(null)

  const query = useQuery({
    queryKey: submitted ? qk.analytics.missedOpportunities(submitted) : ['analytics', 'missed', 'idle'],
    queryFn: ({ signal }) => analyticsApi.missedOpportunities(submitted!, signal),
    enabled: Boolean(submitted),
  })

  const handleQuery = () =>
    setSubmitted({
      limit,
      per_decision_usdc: perDecisionUsdc,
      strategy_id: strategyId ?? undefined,
      since: since ?? undefined,
      until: until ?? undefined,
    })

  const totals = query.data?.totals
  const items = query.data?.items ?? []
  const byReason = query.data?.by_reason ?? []

  const itemColumns: ColumnDef<MissedOpportunityItem, unknown>[] = [
    { header: 'when', cell: ({ row }) => formatIso(row.original.created_at, 'MM-DD HH:mm:ss') },
    {
      header: 'condition / token',
      cell: ({ row }) => (
        <Stack gap={2}>
          <CopyableId value={row.original.condition_id} head={4} tail={4} />
          {row.original.token_id ? <CopyableId value={row.original.token_id} head={4} tail={4} label="tok" /> : null}
        </Stack>
      ),
    },
    {
      header: 'reason',
      cell: ({ row }) => <code style={{ fontSize: 11 }}>{row.original.reason ?? '—'}</code>,
    },
    {
      header: 'entry / fair',
      cell: ({ row }) =>
        `${formatDecimal(row.original.entry_price, { dp: 4 })} / ${formatDecimal(row.original.fair_value, { dp: 4 })}`,
    },
    {
      header: 'status',
      cell: ({ row }) => (
        <StatusPill tone={STATUS_TONE[row.original.status] ?? 'neutral'} size="xs">
          {STATUS_LABEL[row.original.status] ?? row.original.status}
        </StatusPill>
      ),
    },
    {
      header: 'hypothetical PnL',
      cell: ({ row }) => (
        <span style={{ color: pnlToneColor(pnlTone(row.original.hypothetical_pnl_usdc)) }}>
          {formatUsdc(row.original.hypothetical_pnl_usdc)}
        </span>
      ),
    },
  ]

  const reasonColumns: ColumnDef<MissedOpportunityReasonBucket, unknown>[] = [
    { header: 'reason', cell: ({ row }) => <code style={{ fontSize: 11 }}>{row.original.reason}</code> },
    { header: '决策', accessorKey: 'decision_count' },
    { header: '已结算', accessorKey: 'settled_count' },
    {
      header: '本应赚 / 亏',
      cell: ({ row }) => `${row.original.would_have_won_count} / ${row.original.would_have_lost_count}`,
    },
    {
      header: 'hypo PnL 合计',
      cell: ({ row }) => (
        <span style={{ color: pnlToneColor(pnlTone(row.original.hypothetical_pnl_usdc)) }}>
          {formatUsdc(row.original.hypothetical_pnl_usdc)}
        </span>
      ),
    },
    {
      header: '胜率 (已结算)',
      cell: ({ row }) => {
        const settled = row.original.settled_count
        if (!settled) return '—'
        const winRate = row.original.would_have_won_count / settled
        return `${(winRate * 100).toFixed(1)}%`
      },
    },
  ]

  return (
    <>
      <PageHeader
        title="拒绝盈亏模拟 Missed Opportunities"
        subtitle="对 accepted=false 决策按 per_decision_usdc 等额回算；红 = 风控帮你躲过，绿 = 阈值可能过严"
      />
      <Group justify="space-between" mb="md" wrap="wrap">
        <Group gap="sm" wrap="wrap" align="flex-end">
          <TimeWindowPicker />
          <NumberInput
            size="xs"
            label="per_decision_usdc"
            value={perDecisionUsdc}
            onChange={(v) => setPerDecisionUsdc(typeof v === 'number' ? v : 10)}
            min={0.01}
            max={10_000}
            step={1}
            w={140}
          />
          <NumberInput
            size="xs"
            label="limit"
            value={limit}
            onChange={(v) => setLimit(typeof v === 'number' ? v : 500)}
            min={1}
            max={5000}
            step={100}
            w={120}
          />
        </Group>
        <InlineActionButton variant="accent" onClick={handleQuery}>
          查询
        </InlineActionButton>
      </Group>

      {!submitted ? (
        <EmptyState title="点击查询" description="模拟回放只在用户主动触发时跑——窗口越大计算越久。" />
      ) : query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : (
        <Stack gap="md">
          {totals ? (
            <SimpleGrid cols={{ base: 2, md: 5 }} spacing="md">
              <Stat label="拒绝决策数" value={String(totals.decision_count)} />
              <Stat
                label="已结算"
                value={String(totals.settled_count)}
                hint={`占比 ${
                  totals.decision_count
                    ? ((totals.settled_count / totals.decision_count) * 100).toFixed(1)
                    : '0'
                }%`}
              />
              <Stat label="本应赚" value={String(totals.would_have_won_count)} tone="pos" />
              <Stat label="本应亏" value={String(totals.would_have_lost_count)} tone="neg" />
              <Stat
                label="hypo PnL 合计"
                value={formatUsdc(totals.hypothetical_pnl_usdc)}
                tone={pnlTone(totals.hypothetical_pnl_usdc)}
                hint={
                  toDecimal(totals.hypothetical_pnl_usdc)?.isPositive()
                    ? '正数 = 风控可能过严'
                    : '负数 = 风控判断正确'
                }
              />
            </SimpleGrid>
          ) : null}

          <SectionCard title="按 reason 聚合">
            <DataTable<MissedOpportunityReasonBucket>
              columns={reasonColumns}
              data={byReason}
              rowKey={(r) => r.reason}
            />
            <Text size="xs" c="dimmed" mt="xs">
              hypo PnL 大幅为正的 reason → 考虑放宽对应风控；为负则说明该风控规则有效。
            </Text>
          </SectionCard>

          <SectionCard title={`明细 ${items.length} 条 (limit=${query.data?.per_decision_usdc} USDC/决策)`}>
            <DataTable<MissedOpportunityItem>
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


