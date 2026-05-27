import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Group, Select, TextInput } from '@mantine/core'
import { useNavigate } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { marketsApi } from '@core/api/resources'
import type { MarketView } from '@core/api/types'
import { tradingWorkflowBundle } from '@strategy/registry'
import { PageHeader } from '@shared/ui/PageHeader'
import { StatusPill } from '@shared/ui/StatusPill'
import { CopyableId } from '@shared/ui/CopyableId'
import { DimText } from '@shared/ui/MonoCell'
import { DataTable } from '@shared/tables/DataTable'
import { formatIso } from '@shared/format'

const PAGE_SIZE = 100

export function MarketsListPage() {
  const navigate = useNavigate()
  const [page, setPage] = useState(1)
  const [tradingStatus, setTradingStatus] = useState<string | null>(null)
  const [sortBy, setSortBy] = useState<string>('fee_rate_updated_at')
  const [slugSearch, setSlugSearch] = useState('')

  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      trading_status: tradingStatus ?? undefined,
      sort_by: sortBy,
      sort_direction: 'desc' as const,
    }),
    [page, tradingStatus, sortBy],
  )

  const query = useQuery({
    queryKey: qk.markets.list(params),
    queryFn: ({ signal }) => marketsApi.list(params, signal),
  })

  // 策略私有徽章——通用列表不知道当前策略关心什么（持仓 / 暂停 / 高费率…）。
  const renderBadges = tradingWorkflowBundle.marketRowBadges

  const columns: ColumnDef<MarketView, unknown>[] = [
    {
      header: 'market_slug / condition',
      cell: ({ row }) => (
        <div>
          <div style={{ fontSize: 12 }}>{row.original.market_slug}</div>
          <CopyableId value={row.original.condition_id} dense />
        </div>
      ),
    },
    { header: 'category', accessorKey: 'category' },
    { header: 'league', accessorKey: 'league' },
    { header: 'market_type', accessorKey: 'market_type' },
    {
      header: 'status',
      cell: ({ row }) => (
        <StatusPill
          tone={
            row.original.trading_status === 'eligible'
              ? 'success'
              : row.original.trading_status === 'paused'
                ? 'warning'
                : row.original.trading_status === 'rejected'
                  ? 'danger'
                  : 'neutral'
          }
          size="xs"
        >
          {row.original.trading_status ?? '—'}
        </StatusPill>
      ),
    },
    {
      header: 'fee',
      cell: ({ row }) => {
        const f = row.original.fee_preview
        if (!f) return '—'
        return (
          <span style={{ fontSize: 11 }}>
            rate={f.fee_rate_bps ?? '—'} · maker={f.maker_base_fee_bps ?? '—'} · taker={f.taker_base_fee_bps ?? '—'}
          </span>
        )
      },
    },
    {
      header: 'fee updated',
      cell: ({ row }) => formatIso(row.original.fee_preview?.fee_rate_updated_at ?? null, 'MM-DD HH:mm'),
    },
    ...(renderBadges
      ? [
          {
            header: '策略',
            cell: ({ row }: { row: { original: MarketView } }) => {
              const badges = renderBadges(row.original)
              return badges.length === 0 ? (
                <DimText>—</DimText>
              ) : (
                <Group gap={4} wrap="wrap">
                  {badges}
                </Group>
              )
            },
          } as ColumnDef<MarketView, unknown>,
        ]
      : []),
  ]

  const filteredItems = useMemo(() => {
    const items = query.data?.items ?? []
    if (!slugSearch.trim()) return items
    const q = slugSearch.trim().toLowerCase()
    return items.filter(
      (m) =>
        m.market_slug?.toLowerCase().includes(q) ||
        m.condition_id.toLowerCase().includes(q) ||
        m.category?.toLowerCase().includes(q),
    )
  }, [query.data?.items, slugSearch])

  const total = query.data?.total ?? query.data?.items?.length ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <>
      <PageHeader title="市场列表" subtitle="按 fee tier / trading status 过滤；点行进入 timeline" />
      <Group gap="xs" mb="sm" wrap="wrap">
        <TextInput
          size="xs"
          placeholder="搜索 market_slug / condition_id / league"
          value={slugSearch}
          onChange={(e) => { setSlugSearch(e.currentTarget.value); setPage(1) }}
          w={280}
        />
        <Select
          size="xs"
          placeholder="trading_status"
          value={tradingStatus}
          data={['candidate', 'eligible', 'paused', 'closed', 'resolved', 'rejected']}
          onChange={(v) => { setTradingStatus(v); setPage(1) }}
          clearable
          w={150}
        />
        <Select
          size="xs"
          placeholder="sort_by"
          value={sortBy}
          data={[
            'fee_rate_updated_at',
            'fee_rate_bps',
            'maker_base_fee_bps',
            'taker_base_fee_bps',
            'market_slug',
          ]}
          onChange={(v) => v && setSortBy(v)}
          w={200}
        />
      </Group>
      <DataTable<MarketView>
        columns={columns}
        data={filteredItems}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        pagination={{ page, pageCount: totalPages, onChange: setPage }}
        rowKey={(m) => m.condition_id}
        onRowClick={(row) =>
          navigate(`/investigate/timeline?condition_id=${encodeURIComponent(row.original.condition_id)}`)
        }
      />
    </>
  )
}
