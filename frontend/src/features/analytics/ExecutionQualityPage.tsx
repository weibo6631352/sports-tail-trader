import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { SimpleGrid, Stack, Text } from '@mantine/core'
import { qk } from '@core/api/keys'
import { analyticsApi } from '@core/api/resources'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { AnalyticsToolbar } from './AnalyticsToolbar'
import { useAnalyticsParams } from './_useAnalyticsParams'

function fmtMs(v: number | null | undefined): string {
  if (v == null) return '—'
  return `${v.toFixed(1)} ms`
}

function fmtBps(v: number | null | undefined): string {
  if (v == null) return '—'
  return `${v.toFixed(2)} bps`
}

export function ExecutionQualityPage() {
  const params = useAnalyticsParams()
  const [submitted, setSubmitted] = useState<typeof params | null>(null)

  const query = useQuery({
    queryKey: submitted ? qk.analytics.executionQuality(submitted) : ['analytics', 'execq', 'idle'],
    queryFn: ({ signal }) => analyticsApi.executionQuality(submitted ?? {}, signal),
    enabled: Boolean(submitted),
  })

  return (
    <>
      <PageHeader title="执行质量 Execution Quality" subtitle="成交 price vs midpoint 的 slippage 分位" />
      <AnalyticsToolbar loading={query.isFetching} onQuery={() => setSubmitted(params)} />
      {!submitted ? (
        <EmptyState title="点击查询" />
      ) : query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : (
        <SimpleGrid cols={{ base: 1, md: 3 }} spacing="md">
          <SectionCard title="样本">
            <Stack gap={4}>
              <Text size="xl" fw={700}>
                {query.data?.sample_size ?? '—'}
              </Text>
              <Text size="xs" c="dimmed">
                成交数
              </Text>
            </Stack>
          </SectionCard>
          <SectionCard title="Slippage mean">
            <Stack gap={4}>
              <Text size="xl" fw={700}>
                {fmtBps(query.data?.slippage_bps?.mean)}
              </Text>
              <Text size="xs" c="dimmed">
                p95: {fmtBps(query.data?.slippage_bps?.p95)}
              </Text>
            </Stack>
          </SectionCard>
          <SectionCard title="提交延迟 submit latency">
            <Stack gap={4}>
              <Text size="xl" fw={700}>
                p50: {fmtMs(query.data?.submit_latency_ms?.p50)}
              </Text>
              <Text size="xs" c="dimmed">
                p95: {fmtMs(query.data?.submit_latency_ms?.p95)}
              </Text>
            </Stack>
          </SectionCard>
          <SectionCard title="成交延迟 fill latency">
            <Stack gap={4}>
              <Text size="xl" fw={700}>
                p50: {fmtMs(query.data?.fill_latency_ms?.p50)}
              </Text>
              <Text size="xs" c="dimmed">
                p95: {fmtMs(query.data?.fill_latency_ms?.p95)}
              </Text>
            </Stack>
          </SectionCard>
        </SimpleGrid>
      )}
    </>
  )
}
