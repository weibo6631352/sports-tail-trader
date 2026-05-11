import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Checkbox, Group, TextInput } from '@mantine/core'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { candidatesApi } from '@core/api/resources'
import type { LiveSourceGapRow } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { CopyableId } from '@shared/ui/CopyableId'
import { StatusPill } from '@shared/ui/StatusPill'
import { DataTable } from '@shared/tables/DataTable'
import { formatIso } from '@shared/format'

// 当前策略最实用的诊断：哪些 tracking 市场缺直播数据源，按 urgency 排序。
// tail / outright / season-state 的更细诊断需要策略侧端点；这里先用 live-source-gaps。

export function StrategyDiagnosticsPage() {
  const [prefix, setPrefix] = useState('')
  const [includeFuture, setIncludeFuture] = useState(false)

  const params = { limit: 200, prefix: prefix.trim() || undefined, include_future_schedule: includeFuture }
  const query = useQuery({
    queryKey: qk.candidates.liveSourceGaps(params),
    queryFn: ({ signal }) => candidatesApi.liveSourceGaps(params, signal),
  })

  const columns: ColumnDef<LiveSourceGapRow, unknown>[] = [
    {
      header: 'market / event',
      cell: ({ row }) => (
        <div>
          <div style={{ fontSize: 12 }}>{row.original.market_slug ?? row.original.event_slug ?? '—'}</div>
          {row.original.condition_id ? <CopyableId value={row.original.condition_id} dense /> : null}
        </div>
      ),
    },
    {
      header: 'urgency',
      cell: ({ row }) => (
        <StatusPill
          tone={
            row.original.urgency === 'critical'
              ? 'danger'
              : row.original.urgency === 'high'
                ? 'warning'
                : row.original.urgency === 'medium'
                  ? 'info'
                  : 'neutral'
          }
          size="xs"
        >
          {row.original.urgency ?? '—'}
        </StatusPill>
      ),
    },
    { header: 'prefix', accessorKey: 'prefix' },
    {
      header: 'expected start',
      cell: ({ row }) => formatIso(row.original.expected_start_at, 'MM-DD HH:mm'),
    },
    {
      header: 'last live_state',
      cell: ({ row }) => formatIso(row.original.last_live_state_at, 'MM-DD HH:mm'),
    },
    {
      header: 'reason',
      cell: ({ row }) =>
        row.original.reason ? <code style={{ fontSize: 11 }}>{row.original.reason}</code> : '—',
    },
  ]

  return (
    <>
      <PageHeader
        title="策略诊断 · Live source gaps"
        subtitle="tracking 市场中缺少直播数据源的清单——按 urgency 优先处理"
      />
      <Group gap="xs" mb="sm" wrap="wrap">
        <TextInput
          size="xs"
          placeholder="prefix (例如 'nba')"
          value={prefix}
          onChange={(e) => setPrefix(e.currentTarget.value)}
          w={200}
        />
        <Checkbox
          size="xs"
          label="include_future_schedule"
          checked={includeFuture}
          onChange={(e) => setIncludeFuture(e.currentTarget.checked)}
        />
      </Group>
      <DataTable<LiveSourceGapRow>
        columns={columns}
        data={query.data?.items}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        rowKey={(r, i) => `${r.condition_id ?? r.market_slug ?? i}`}
      />
    </>
  )
}
