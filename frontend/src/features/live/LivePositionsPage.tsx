import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { notifications } from '@mantine/notifications'
import type { ColumnDef } from '@tanstack/react-table'
import { qk, qkRoots } from '@core/api/keys'
import { positionsApi } from '@core/api/resources'
import type { PositionRow } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { CopyableId } from '@shared/ui/CopyableId'
import { StatusPill } from '@shared/ui/StatusPill'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { DataTable } from '@shared/tables/DataTable'
import { confirmAction } from '@shared/forms/confirmAction'
import { describeError } from '@core/api/errors'
import { formatDecimal, formatUsdc, pnlTone, pnlToneColor } from '@shared/format'

const PAGE_SIZE = 100

export function LivePositionsPage() {
  const client = useQueryClient()
  const [page, setPage] = useState(1)
  const params = { limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE }
  const query = useQuery({
    queryKey: qk.positions.list(params),
    queryFn: ({ signal }) => positionsApi.list(params, signal),
  })

  const forceExit = useMutation({
    mutationFn: positionsApi.forceExit,
    onSuccess: () => {
      notifications.show({ title: 'force exit 已提交', message: '后续以 audit_events 为准', color: 'teal' })
      client.invalidateQueries({ queryKey: qkRoots.positions })
      client.invalidateQueries({ queryKey: qkRoots.orders })
      client.invalidateQueries({ queryKey: qkRoots.auditEvents })
    },
    onError: (err) => {
      notifications.show({ title: 'force exit 失败', message: describeError(err), color: 'red' })
    },
  })

  const columns: ColumnDef<PositionRow, unknown>[] = useMemo(
    () => [
      {
        header: 'market / token',
        cell: ({ row }) => (
          <div>
            <div style={{ fontSize: 12 }}>{row.original.market_slug ?? '—'}</div>
            <CopyableId value={row.original.token_id} head={4} tail={4} />
          </div>
        ),
      },
      {
        header: 'size',
        cell: ({ row }) => formatDecimal(row.original.size_shares, { dp: 2 }),
      },
      {
        header: 'cost',
        cell: ({ row }) => formatUsdc(row.original.cost_usdc),
      },
      {
        header: 'entry / current',
        cell: ({ row }) => (
          <span>
            {formatDecimal(row.original.entry_price, { dp: 4 })} → {formatDecimal(row.original.current_price, { dp: 4 })}
          </span>
        ),
      },
      {
        header: 'cash PnL',
        cell: ({ row }) => (
          <span style={{ color: pnlToneColor(pnlTone(row.original.cash_pnl)) }}>
            {formatUsdc(row.original.cash_pnl)}
          </span>
        ),
      },
      {
        header: 'realized',
        cell: ({ row }) => (
          <span style={{ color: pnlToneColor(pnlTone(row.original.realized_pnl)) }}>
            {formatUsdc(row.original.realized_pnl)}
          </span>
        ),
      },
      {
        header: '%',
        cell: ({ row }) => (
          <span style={{ color: pnlToneColor(pnlTone(row.original.percent_pnl)) }}>
            {formatDecimal(row.original.percent_pnl, { dp: 2, signed: true, suffix: '%' })}
          </span>
        ),
      },
      {
        header: '状态',
        cell: ({ row }) => (
          <StatusPill tone={row.original.redeemable ? 'success' : 'neutral'} size="xs">
            {row.original.redeemable ? 'redeemable' : 'open'}
          </StatusPill>
        ),
      },
      {
        header: '操作',
        cell: ({ row }) => {
          const p = row.original
          return (
            <InlineActionButton
              variant="danger"
              onClick={(e) => {
                e.stopPropagation()
                confirmAction({
                  title: `Force Exit · ${p.market_slug ?? p.condition_id}`,
                  description: '将以 market 价格强制平仓；不可撤销。',
                  tone: 'danger',
                  diff: [
                    {
                      field: 'size_shares',
                      before: p.size_shares,
                      after: '0',
                      risk: 'high',
                    },
                  ],
                  onConfirm: async ({ operator, reason, trace_id }) => {
                    await forceExit.mutateAsync({
                      condition_id: p.condition_id,
                      token_id: p.token_id,
                      operator,
                      reason,
                      trace_id,
                    })
                  },
                })
              }}
            >
              force exit
            </InlineActionButton>
          )
        },
      },
    ],
    [forceExit],
  )

  const totalPages = Math.max(1, Math.ceil((query.data?.total ?? query.data?.items?.length ?? 0) / PAGE_SIZE))

  return (
    <>
      <PageHeader title="实时持仓" subtitle="行内 force exit · 二次确认 + operator/reason" />
      <DataTable<PositionRow>
        columns={columns}
        data={query.data?.items}
        isLoading={query.isLoading}
        isFetching={query.isFetching}
        error={query.error}
        onRefresh={() => query.refetch()}
        pagination={{ page, pageCount: totalPages, onChange: setPage }}
        rowKey={(p) => `${p.condition_id}_${p.token_id}`}
      />
    </>
  )
}

