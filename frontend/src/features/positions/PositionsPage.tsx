import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { formatApiError } from '../../core/api/client'
import { adminApi } from '../../core/api/resources'
import type { FillRecord, PositionRecord } from '../../core/api/types'
import { SectionCard } from '../../shared/ui/SectionCard'
import { DataTable, type DataColumn } from '../../shared/ui/DataTable'
import { MarketExternalLink } from '../../shared/ui/MarketExternalLink'
import { QueryErrorNotice } from '../../shared/ui/QueryErrorNotice'
import { StatusPill } from '../../shared/ui/StatusPill'
import { formatDateTime, formatDecimal } from '../../shared/utils/format'
import { formatConfirmationStatusLabel, formatOrderSideLabel, isSellOrderSide } from '../../shared/utils/labels'

export const PositionsPage = () => {
  const queryClient = useQueryClient()
  const [conditionId, setConditionId] = useState('')
  const [tokenId, setTokenId] = useState('')
  const [fillTraceId, setFillTraceId] = useState('')
  const [forceExitOperator, setForceExitOperator] = useState('人工')

  const forceExitMutation = useMutation({
    mutationFn: (payload: { condition_id: string; token_id: string; operator: string }) =>
      adminApi.forceExitPosition(payload),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['positions'] }),
        queryClient.invalidateQueries({ queryKey: ['orders'] }),
        queryClient.invalidateQueries({ queryKey: ['portfolio'] }),
      ])
    },
  })
  const forceExitError = forceExitMutation.error ? formatApiError(forceExitMutation.error) : null
  const handleForceExit = (row: PositionRecord) => {
    const normalizedOperator = forceExitOperator.trim() || 'manual'
    if (!window.confirm(`将以当前 best_bid 强平 ${row.market_slug ?? row.condition_id} / ${row.token_id}，是否继续？`)) return
    forceExitMutation.mutate({
      condition_id: row.condition_id,
      token_id: row.token_id,
      operator: normalizedOperator,
    })
  }

  const positionsQuery = useQuery({
    queryKey: ['positions', { conditionId, tokenId }],
    queryFn: () =>
      adminApi.listPositions({
        limit: 100,
        offset: 0,
        condition_id: conditionId || undefined,
        token_id: tokenId || undefined,
      }),
    refetchInterval: 10_000,
  })

  const fillsQuery = useQuery({
    queryKey: ['fills', { fillTraceId, conditionId, tokenId }],
    queryFn: () =>
      adminApi.listFills({
        limit: 100,
        offset: 0,
        trace_id: fillTraceId || undefined,
        condition_id: conditionId || undefined,
        token_id: tokenId || undefined,
      }),
    refetchInterval: 10_000,
  })

  const positionColumns: Array<DataColumn<PositionRecord>> = [
    {
      key: 'market',
      header: '市场',
      cell: (row) => (
        <div className="table-primary">
          {row.market_slug ? (
            <MarketExternalLink className="market-list__title" eventSlug={row.event_slug}>
              {row.market_slug}
            </MarketExternalLink>
          ) : (
            <strong>{row.condition_id}</strong>
          )}
          <span>{row.token_id}</span>
        </div>
      ),
    },
    { key: 'shares', header: '持仓份额', align: 'right', cell: (row) => formatDecimal(row.shares) },
    { key: 'cost', header: '持仓成本', align: 'right', cell: (row) => formatDecimal(row.cost_usdc) },
    { key: 'currentValue', header: '当前价值', align: 'right', cell: (row) => formatDecimal(row.current_value) },
    { key: 'cashPnl', header: '现金盈亏', align: 'right', cell: (row) => formatDecimal(row.cash_pnl) },
    { key: 'realizedPnl', header: '已实现盈亏', align: 'right', cell: (row) => formatDecimal(row.realized_pnl) },
    {
      key: 'confirmed',
      header: '已确认份额',
      align: 'right',
      cell: (row) => formatDecimal(row.confirmed_shares),
    },
    {
      key: 'status',
      header: '确认状态',
      cell: (row) => <StatusPill label={formatConfirmationStatusLabel(row.confirmation_status)} tone="neutral" />,
    },
    { key: 'updated', header: '更新时间', cell: (row) => formatDateTime(row.updated_at) },
    {
      key: 'actions',
      header: '操作',
      cell: (row) => {
        const isExiting =
          forceExitMutation.isPending &&
          forceExitMutation.variables?.condition_id === row.condition_id &&
          forceExitMutation.variables?.token_id === row.token_id
        return (
          <button
            type="button"
            disabled={forceExitMutation.isPending || !row.shares || Number(row.shares) <= 0}
            onClick={() => handleForceExit(row)}
          >
            {isExiting ? '平仓中...' : '强平'}
          </button>
        )
      },
    },
  ]

  const fillColumns: Array<DataColumn<FillRecord>> = [
    {
      key: 'market',
      header: '市场',
      cell: (row) => (
        <div className="table-primary">
          {row.market_slug ? (
            <MarketExternalLink className="market-list__title" eventSlug={row.event_slug}>
              {row.market_slug}
            </MarketExternalLink>
          ) : (
            <strong>{row.condition_id}</strong>
          )}
          <span>{row.trade_id ?? row.order_id ?? '—'}</span>
        </div>
      ),
    },
    {
      key: 'side',
      header: '方向',
      cell: (row) => (
        <StatusPill label={formatOrderSideLabel(row.side)} tone={isSellOrderSide(row.side) ? 'warning' : 'neutral'} />
      ),
    },
    { key: 'price', header: '价格', align: 'right', cell: (row) => formatDecimal(row.price) },
    { key: 'size', header: '数量', align: 'right', cell: (row) => formatDecimal(row.size) },
    { key: 'notional', header: '名义金额', align: 'right', cell: (row) => formatDecimal(row.notional_usdc) },
    { key: 'created', header: '创建时间', cell: (row) => formatDateTime(row.created_at) },
  ]

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">持仓</p>
          <h1>持仓与成交记录</h1>
          <p>这里统一查看持仓快照和成交记录，便于核对仓位变化。</p>
        </div>
      </header>

      <SectionCard title="过滤条件" subtitle="持仓和成交记录分别查询。">
        <div className="form-grid form-grid--filters">
          <label>
            <span>条件 ID</span>
            <input value={conditionId} onChange={(event) => setConditionId(event.target.value)} />
          </label>
          <label>
            <span>代币 ID</span>
            <input value={tokenId} onChange={(event) => setTokenId(event.target.value)} />
          </label>
          <label>
            <span>成交追踪 ID</span>
            <input value={fillTraceId} onChange={(event) => setFillTraceId(event.target.value)} />
          </label>
          <label>
            <span>强平操作者</span>
            <input value={forceExitOperator} onChange={(event) => setForceExitOperator(event.target.value)} />
          </label>
        </div>
        {forceExitError ? (
          <ul className="message-list form-feedback">
            <li>
              <strong>强平失败</strong>
              <span>{forceExitError}</span>
            </li>
          </ul>
        ) : null}
      </SectionCard>

      <SectionCard title="持仓" subtitle={`当前 ${positionsQuery.data?.total ?? 0} 条。`}>
        {positionsQuery.error ? (
          <QueryErrorNotice title="持仓加载失败" error={positionsQuery.error} />
        ) : (
          <DataTable
            columns={positionColumns}
            rows={positionsQuery.data?.items ?? []}
            rowKey={(row) => `${row.condition_id}-${row.token_id}`}
            emptyTitle="没有持仓"
            emptyDescription="当前没有匹配的持仓记录。"
          />
        )}
      </SectionCard>

      <SectionCard title="成交记录" subtitle={`当前 ${fillsQuery.data?.total ?? 0} 条。`}>
        {fillsQuery.error ? (
          <QueryErrorNotice title="成交记录加载失败" error={fillsQuery.error} />
        ) : (
          <DataTable
            columns={fillColumns}
            rows={fillsQuery.data?.items ?? []}
            rowKey={(row) => row.event_id ?? `${row.order_id}-${row.trade_id}-${row.created_at ?? 'unknown'}`}
            emptyTitle="没有成交记录"
            emptyDescription="当前没有匹配的成交记录。"
          />
        )}
      </SectionCard>
    </div>
  )
}
