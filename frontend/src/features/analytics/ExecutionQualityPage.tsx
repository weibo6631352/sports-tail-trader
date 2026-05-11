import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { SimpleGrid, Stack, Text } from '@mantine/core'
import { qk } from '@core/api/keys'
import { analyticsApi } from '@core/api/resources'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { formatBps, formatDecimal } from '@shared/format'
import { AnalyticsToolbar } from './AnalyticsToolbar'
import { useAnalyticsParams } from './_useAnalyticsParams'

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
                {query.data?.fill_count ?? '—'}
              </Text>
              <Text size="xs" c="dimmed">
                成交数
              </Text>
            </Stack>
          </SectionCard>
          <SectionCard title="平均 slippage">
            <Stack gap={4}>
              <Text size="xl" fw={700}>
                {formatBps(query.data?.avg_slippage_bps)}
              </Text>
              <Text size="xs" c="dimmed">
                {formatDecimal(query.data?.avg_slippage_bps, { dp: 2, suffix: ' bps' })}
              </Text>
            </Stack>
          </SectionCard>
          <SectionCard title="中位数 slippage">
            <Stack gap={4}>
              <Text size="xl" fw={700}>
                {formatBps(query.data?.median_slippage_bps)}
              </Text>
            </Stack>
          </SectionCard>

          {(query.data?.per_market_type ?? []).length > 0 && (
            <SectionCard title="按 market_type" style={{ gridColumn: '1 / -1' }}>
              <Stack gap={4}>
                {query.data!.per_market_type!.map((row) => (
                  <Stack key={row.market_type} gap={2}>
                    <Text size="sm">{row.market_type}</Text>
                    <Text size="xs" c="dimmed">
                      fills={row.fill_count} · avg_slippage={formatBps(row.avg_slippage_bps)}
                    </Text>
                  </Stack>
                ))}
              </Stack>
            </SectionCard>
          )}
        </SimpleGrid>
      )}
    </>
  )
}
