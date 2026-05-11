import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, NumberInput, SimpleGrid, Stack, Text, TextInput, Collapse } from '@mantine/core'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { qk } from '@core/api/keys'
import { analyticsApi } from '@core/api/resources'
import type { RiskCheck, RiskRejectionEvent } from '@core/api/types'
import { chartTooltipStyle } from '@shared/charts'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { CopyableId } from '@shared/ui/CopyableId'
import { StatusPill } from '@shared/ui/StatusPill'
import { TimeWindowPicker } from '@shared/time/TimeWindowPicker'
import { formatIso } from '@shared/format'
import { useTimeWindowStore } from '@core/time/store'

// 风控结构化拒绝：
//   - 聚合 (/aggregate)：按 check_name + failed_field 直方图
//   - 逐条 (list)：每条 audit_event 含 payload.checks[]（name/field/value/passed/suggested_action）

export function RiskRejectionsPage() {
  const since = useTimeWindowStore((s) => s.since)
  const until = useTimeWindowStore((s) => s.until)
  const [conditionId, setConditionId] = useState('')
  const [sampleLimit, setSampleLimit] = useState(1000)
  const [submitted, setSubmitted] = useState<{
    sample_limit: number
    condition_id?: string
    since?: number
    until?: number
  } | null>(null)

  const aggregate = useQuery({
    queryKey: submitted ? qk.analytics.riskRejectionsAggregate(submitted) : ['analytics', 'risk-aggregate', 'idle'],
    queryFn: ({ signal }) => analyticsApi.riskRejectionsAggregate(submitted!, signal),
    enabled: Boolean(submitted),
  })

  const list = useQuery({
    queryKey: submitted
      ? qk.analytics.riskRejections({
          limit: 200,
          condition_id: submitted.condition_id,
          since: submitted.since,
          until: submitted.until,
        })
      : ['analytics', 'risk-list', 'idle'],
    queryFn: ({ signal }) =>
      analyticsApi.riskRejections(
        {
          limit: 200,
          condition_id: submitted!.condition_id,
          since: submitted!.since,
          until: submitted!.until,
        },
        signal,
      ),
    enabled: Boolean(submitted),
  })

  const handleQuery = () =>
    setSubmitted({
      sample_limit: sampleLimit,
      condition_id: conditionId.trim() || undefined,
      since: since ?? undefined,
      until: until ?? undefined,
    })

  return (
    <>
      <PageHeader
        title="风控结构化拒绝 Risk Rejections"
        subtitle="按 check_name / failed_field 聚合，互补 /analytics/rejections 的 reason 字符串 top"
      />

      <Group justify="space-between" mb="md" wrap="wrap">
        <Group gap="sm" wrap="wrap" align="flex-end">
          <TimeWindowPicker />
          <TextInput
            size="xs"
            label="condition_id (可选)"
            value={conditionId}
            onChange={(e) => setConditionId(e.currentTarget.value)}
            w={320}
          />
          <NumberInput
            size="xs"
            label="sample_limit"
            value={sampleLimit}
            onChange={(v) => setSampleLimit(typeof v === 'number' ? v : 1000)}
            min={1}
            max={5000}
            step={200}
            w={140}
          />
        </Group>
        <InlineActionButton variant="accent" onClick={handleQuery}>
          查询
        </InlineActionButton>
      </Group>

      {!submitted ? (
        <EmptyState title="点击查询" />
      ) : (
        <Stack gap="md">
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
            <SectionCard title="按 check_name 聚合">
              {aggregate.error ? (
                <QueryErrorNotice error={aggregate.error} compact />
              ) : (
                <BucketChart
                  data={aggregate.data?.by_check_name?.map((b) => ({ key: b.check_name, count: b.count })) ?? []}
                  totalRejections={aggregate.data?.total_rejections ?? 0}
                />
              )}
            </SectionCard>
            <SectionCard title="按 failed_field 聚合">
              {aggregate.error ? (
                <QueryErrorNotice error={aggregate.error} compact />
              ) : (
                <BucketChart
                  data={aggregate.data?.by_failed_field?.map((b) => ({ key: b.field, count: b.count })) ?? []}
                  totalRejections={aggregate.data?.total_rejections ?? 0}
                />
              )}
            </SectionCard>
          </SimpleGrid>

          <SectionCard title={`逐条 (limit=200) · ${list.data?.items?.length ?? 0} 条`}>
            {list.error ? (
              <QueryErrorNotice error={list.error} onRetry={() => list.refetch()} />
            ) : (
              <RiskRejectionList rows={list.data?.items ?? []} loading={list.isLoading} />
            )}
          </SectionCard>
        </Stack>
      )}
    </>
  )
}

function BucketChart({
  data,
  totalRejections,
}: {
  data: Array<{ key: string; count: number }>
  totalRejections: number
}) {
  if (data.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        无样本（total_rejections={totalRejections}）。
      </Text>
    )
  }
  return (
    <>
      <div style={{ width: '100%', height: Math.min(360, 30 + 28 * data.length) }}>
        <ResponsiveContainer>
          <BarChart data={data} layout="vertical" margin={{ left: 80 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#243352" />
            <XAxis type="number" stroke="#97a6c2" tick={{ fontSize: 11 }} />
            <YAxis
              type="category"
              dataKey="key"
              stroke="#97a6c2"
              tick={{ fontSize: 11 }}
              width={120}
            />
            <Tooltip contentStyle={chartTooltipStyle} />
            <Bar dataKey="count" fill="#f0b955" />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <Text size="xs" c="dimmed" mt="xs">
        合计 {totalRejections} 次拒绝；下方逐条带完整 checks[] 投影。
      </Text>
    </>
  )
}

function RiskRejectionList({ rows, loading }: { rows: RiskRejectionEvent[]; loading: boolean }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  if (loading && rows.length === 0) {
    return (
      <Text c="dimmed" size="sm">
        加载中…
      </Text>
    )
  }

  if (rows.length === 0) {
    return (
      <Text c="dimmed" size="sm">
        窗口内无风控拒绝事件。
      </Text>
    )
  }

  // 弃用 DataTable + Collapse 双层结构（前者展开块会堆在表底）。
  // 改成 Stack of expandable cards：紧贴每一行的 checks 详情就展开在那一行下面。
  return (
    <Stack gap={6}>
      {rows.map((row) => (
        <RiskRejectionCard
          key={row.event_id}
          event={row}
          expanded={expanded.has(row.event_id)}
          onToggle={() => toggle(row.event_id)}
        />
      ))}
    </Stack>
  )
}

function RiskRejectionCard({
  event,
  expanded,
  onToggle,
}: {
  event: RiskRejectionEvent
  expanded: boolean
  onToggle: () => void
}) {
  const failed = (event.payload?.checks ?? []).filter((c) => c.passed === false)
  return (
    <SectionCard>
      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <Stack gap={2} style={{ flex: 1, minWidth: 0 }}>
          <Group gap="xs" wrap="wrap">
            <Text size="xs" c="dimmed" ff="var(--font-mono)">
              {formatIso(event.created_at, 'MM-DD HH:mm:ss')}
            </Text>
            <CopyableId value={event.condition_id ?? ''} dense />
            <CopyableId value={event.trace_id ?? ''} dense label="trace" />
            <code style={{ fontSize: 11, color: 'var(--color-text-dim)' }}>
              {event.reason ?? '—'}
            </code>
          </Group>
          <Group gap={4} wrap="wrap">
            {failed.length === 0 ? (
              <Text size="xs" c="dimmed">
                无 failed checks
              </Text>
            ) : (
              failed.map((c, i) => (
                <StatusPill key={`${c.name}_${i}`} tone="danger" size="xs">
                  {c.name ?? '(unnamed)'}
                  {c.field ? `: ${c.field}` : ''}
                </StatusPill>
              ))
            )}
          </Group>
        </Stack>
        <InlineActionButton variant="link" onClick={onToggle}>
          {expanded ? '收起' : `展开 checks (${event.payload?.checks?.length ?? 0})`}
        </InlineActionButton>
      </Group>
      <Collapse in={expanded}>
        <Stack gap={4} mt="sm" pt="sm" style={{ borderTop: '1px dashed var(--color-border)' }}>
          {(event.payload?.checks ?? []).map((check, i) => (
            <CheckRow key={`${event.event_id}_${i}`} check={check} />
          ))}
          {(event.payload?.checks ?? []).length === 0 ? (
            <Text size="xs" c="dimmed">
              该事件 payload 无 checks 字段。
            </Text>
          ) : null}
        </Stack>
      </Collapse>
    </SectionCard>
  )
}

function CheckRow({ check }: { check: RiskCheck }) {
  return (
    <Group gap="sm" wrap="nowrap" align="flex-start">
      <StatusPill tone={check.passed === false ? 'danger' : check.passed === true ? 'success' : 'neutral'} size="xs">
        {check.passed === false ? 'FAIL' : check.passed === true ? 'pass' : '?'}
      </StatusPill>
      <Stack gap={0} style={{ flex: 1 }}>
        <Text size="sm" ff="var(--font-mono)">
          {check.name ?? '(unnamed)'}
          {check.field ? ` · ${check.field}` : ''}
        </Text>
        <Text size="xs" c="dimmed">
          value: <code>{stringify(check.value)}</code>
          {check.suggested_action ? ` · 建议：${check.suggested_action}` : ''}
        </Text>
      </Stack>
    </Group>
  )
}

function stringify(value: unknown): string {
  if (value === null || value === undefined) return '∅'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

