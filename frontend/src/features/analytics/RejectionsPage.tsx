import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { analyticsApi } from '@core/api/resources'
import type { RejectionBucket } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { DataTable } from '@shared/tables/DataTable'
import { AnalyticsToolbar } from './AnalyticsToolbar'
import { useAnalyticsParams } from './_useAnalyticsParams'

export function RejectionsPage() {
  const params = useAnalyticsParams()
  const [submitted, setSubmitted] = useState<typeof params | null>(null)

  const query = useQuery({
    queryKey: submitted ? qk.analytics.rejections(submitted) : ['analytics', 'rejections', 'idle'],
    queryFn: ({ signal }) => analyticsApi.rejections(submitted ?? {}, signal),
    enabled: Boolean(submitted),
  })

  const columns: ColumnDef<RejectionBucket, unknown>[] = [
    { header: 'reason', accessorKey: 'reason' },
    { header: 'count', accessorKey: 'count' },
    {
      header: 'pct',
      cell: ({ row }) => (typeof row.original.pct === 'number' ? `${(row.original.pct * 100).toFixed(2)}%` : '—'),
    },
    { header: 'market_type', accessorKey: 'market_type' },
    { header: 'league', accessorKey: 'league' },
  ]

  return (
    <>
      <PageHeader title="拒绝原因 Top" subtitle="按 reason 桶聚合；可继续在 league / market_type 切片" />
      <AnalyticsToolbar loading={query.isFetching} onQuery={() => setSubmitted(params)} />
      {!submitted ? (
        <EmptyState title="点击查询" />
      ) : query.error ? (
        <QueryErrorNotice error={query.error} onRetry={() => query.refetch()} />
      ) : (
        <DataTable<RejectionBucket>
          columns={columns}
          data={query.data?.rejections}
          isLoading={query.isLoading}
          rowKey={(r, i) => `${r.reason}_${i}`}
        />
      )}
    </>
  )
}
