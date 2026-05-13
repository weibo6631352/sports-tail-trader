import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, Text } from '@mantine/core'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { analyticsApi } from '@core/api/resources'
import type { FunnelStage } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { DataTable } from '@shared/tables/DataTable'
import { chartTooltipStyle } from '@shared/charts'
import { AnalyticsToolbar } from './AnalyticsToolbar'
import { useAnalyticsParams } from './_useAnalyticsParams'

export function FunnelPage() {
  const params = useAnalyticsParams()
  const [submitted, setSubmitted] = useState<typeof params | null>(null)

  const query = useQuery({
    queryKey: submitted ? qk.analytics.funnel(submitted) : ['analytics', 'funnel', 'idle'],
    queryFn: ({ signal }) => analyticsApi.funnel(submitted ?? {}, signal),
    enabled: Boolean(submitted),
  })

  const columns: ColumnDef<FunnelStage, unknown>[] = [
    { header: 'stage', accessorKey: 'name' },
    { header: 'count', accessorKey: 'count' },
  ]

  return (
    <>
      <PageHeader title="决策漏斗 Funnel" subtitle="scanned → ranked → scored → sized → decided → routed → acknowledged" />
      <AnalyticsToolbar loading={query.isFetching} onQuery={() => setSubmitted(params)} />

      {!submitted ? (
        <EmptyState title="选择时间窗口后点击查询" description="所有分析页均为手动触发，避免恒定打 DB" />
      ) : query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : (
        <>
          <SectionCard title="柱状图">
            <div style={{ width: '100%', height: 240 }}>
              <ResponsiveContainer>
                <BarChart data={query.data?.stages ?? []}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#243352" />
                  <XAxis dataKey="name" stroke="#97a6c2" tick={{ fontSize: 11 }} />
                  <YAxis stroke="#97a6c2" tick={{ fontSize: 11 }} />
                  <Tooltip contentStyle={chartTooltipStyle} />
                  <Bar dataKey="count" fill="#5cd9c5" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </SectionCard>
          <SectionCard title="明细">
            <DataTable<FunnelStage>
              columns={columns}
              data={query.data?.stages}
              isLoading={query.isLoading}
              error={query.error}
              rowKey={(r) => r.name}
            />
          </SectionCard>
          {query.data?.window_ms ? (
            <Group justify="flex-end" mt="xs">
              <Text size="xs" c="dimmed">
                window={(query.data.window_ms / 1000 / 3600).toFixed(1)} h
              </Text>
            </Group>
          ) : null}
        </>
      )}
    </>
  )
}
