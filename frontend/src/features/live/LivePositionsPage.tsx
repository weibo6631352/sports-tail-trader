import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { notifications } from '@mantine/notifications'
import { Alert, Anchor, Checkbox, Group, Text } from '@mantine/core'
import { useNavigate } from 'react-router-dom'
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
  const navigate = useNavigate()
  const [page, setPage] = useState(1)
  const [activeOnly, setActiveOnly] = useState(false)
  // level=detail 拿到 cash_pnl / percent_pnl / realized_pnl / open_buy_shares 等;
  // 5s 轮询保证持仓页准实时, 行内字段随盘口走 reconcile authority refresh.
  const params = {
    level: 'detail' as const,
    limit: PAGE_SIZE,
    offset: (page - 1) * PAGE_SIZE,
  }
  const query = useQuery({
    queryKey: qk.positions.list(params),
    queryFn: ({ signal }) => positionsApi.list(params, signal),
    refetchInterval: 5000,
  })

  const forceExit = useMutation({
    mutationFn: positionsApi.forceExit,
    onSuccess: (data) => {
      if (data.status === 'failed') {
        notifications.show({
          title: 'force exit 失败',
          message: data.reason ?? 'market_not_operable',
          color: 'red',
        })
        return
      }
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
        header: 'market / outcome',
        cell: ({ row }) => (
          <div>
            <Anchor
              size="xs"
              onClick={(e) => {
                e.stopPropagation()
                navigate(`/investigate/timeline?condition_id=${encodeURIComponent(row.original.condition_id)}`)
              }}
              style={{ cursor: 'pointer', fontSize: 12 }}
            >
              {row.original.market_slug ?? row.original.condition_id}
            </Anchor>
            <div style={{ fontSize: 11, color: 'var(--mantine-color-dimmed)' }}>
              {row.original.outcome ?? ''}
            </div>
            <CopyableId value={row.original.token_id} dense />
          </div>
        ),
      },
      {
        header: 'shares',
        cell: ({ row }) => formatDecimal(row.original.shares, { dp: 2 }),
      },
      {
        header: 'cost',
        cell: ({ row }) => formatUsdc(row.original.cost_usdc),
      },
      {
        header: 'cur price',
        cell: ({ row }) => formatDecimal(row.original.cur_price, { dp: 4 }),
      },
      {
        header: 'bid / ask',
        cell: ({ row }) => (
          <span style={{ fontSize: 12 }}>
            {formatDecimal(row.original.best_bid, { dp: 3 })} / {formatDecimal(row.original.best_ask, { dp: 3 })}
          </span>
        ),
      },
      {
        header: 'mtm value',
        cell: ({ row }) => formatUsdc(row.original.position_usdc),
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
        header: '%',
        cell: ({ row }) => (
          <span style={{ color: pnlToneColor(pnlTone(row.original.percent_pnl)) }}>
            {formatDecimal(row.original.percent_pnl, { dp: 2, signed: true, suffix: '%' })}
          </span>
        ),
      },
      {
        header: '状态',
        cell: ({ row }) => {
          // 状态优先级: settled_zero_value(已赎归零) > redeemable(待赎)
          //   > is_paused(MANUAL pause) > open buy/sell > open
          const p = row.original
          if (p.settled_zero_value) return <StatusPill tone="neutral" size="xs">归零</StatusPill>
          if (p.redeemable) return <StatusPill tone="warning" size="xs">待赎回</StatusPill>
          if (p.is_paused) return <StatusPill tone="warning" size="xs">人工暂停</StatusPill>
          if (p.has_open_buy) return <StatusPill tone="info" size="xs">挂买中</StatusPill>
          if (p.has_open_sell) return <StatusPill tone="info" size="xs">挂卖中</StatusPill>
          return <StatusPill tone="success" size="xs">持有</StatusPill>
        },
      },
      {
        header: '操作',
        cell: ({ row }) => {
          const p = row.original
          if (p.redeemable) return <span style={{ color: 'var(--mantine-color-dimmed)', fontSize: 12 }}>待赎回</span>
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
                      field: 'shares',
                      before: p.shares,
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
    [forceExit, navigate],
  )

  // 后端 /positions 包络 {positions, count}, 不是 Page<T> 的 {items, total}.
  const allItems = query.data?.positions ?? []
  const redeemableCount = allItems.filter((p) => p.redeemable).length
  const displayItems = activeOnly ? allItems.filter((p) => !p.redeemable) : allItems
  const totalPages = Math.max(1, Math.ceil((query.data?.count ?? allItems.length) / PAGE_SIZE))

  return (
    <>
      <PageHeader
        title="实时持仓"
        subtitle="OPEN = 市场进行中；REDEEMABLE = 市场已结算，代币待赎回（价值可能为 $0）· force exit 需二次确认"
      />
      {redeemableCount > 0 && (
        <Alert color="orange" mb="sm" variant="light">
          <Text size="sm">
            <strong>{redeemableCount} 个持仓已结算（REDEEMABLE）</strong>——市场已结束，你持有的代币需要发起赎回交易才能从账户清除。
            若押注方向错误，赎回金额为 $0；若押注正确，赎回可拿回对应金额。这些不是"亏损中的活跃持仓"，而是等待链上清算的已结算头寸。
          </Text>
        </Alert>
      )}
      <Group mb="sm">
        <Checkbox
          size="xs"
          label="只看活跃（OPEN）"
          checked={activeOnly}
          onChange={(e) => { setActiveOnly(e.currentTarget.checked); setPage(1) }}
        />
      </Group>
      <DataTable<PositionRow>
        columns={columns}
        data={displayItems}
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

