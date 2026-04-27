import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { adminApi } from '../../core/api/resources'
import type { OrderRecord } from '../../core/api/types'
import { formatApiError } from '../../core/api/client'
import { SectionCard } from '../../shared/ui/SectionCard'
import { DataTable, type DataColumn } from '../../shared/ui/DataTable'
import { JsonPanel } from '../../shared/ui/JsonPanel'
import { MarketExternalLink } from '../../shared/ui/MarketExternalLink'
import { StatusPill } from '../../shared/ui/StatusPill'
import { formatDateTime, formatDecimal } from '../../shared/utils/format'
import { formatOrderSideLabel, formatOrderStatusLabel, isSellOrderSide } from '../../shared/utils/labels'

const orderTone = (status: string): 'neutral' | 'success' | 'warning' | 'danger' => {
  const normalized = status.trim().toLowerCase()
  if (normalized === 'matched' || normalized === 'partially_filled') {
    return 'success'
  }
  if (
    normalized === 'created' ||
    normalized === 'signed' ||
    normalized === 'submitted' ||
    normalized === 'cancel_requested' ||
    normalized === 'live' ||
    normalized === 'placed'
  ) {
    return 'warning'
  }
  if (normalized === 'failed' || normalized === 'rejected' || normalized === 'cancelled') {
    return 'danger'
  }
  return 'neutral'
}

export const OrdersPage = () => {
  const queryClient = useQueryClient()
  const [openOnly, setOpenOnly] = useState(true)
  const [conditionId, setConditionId] = useState('')
  const [tokenId, setTokenId] = useState('')
  const [traceId, setTraceId] = useState('')
  const [offset, setOffset] = useState(0)
  const [selectedOrder, setSelectedOrder] = useState<OrderRecord | null>(null)
  const [marketSlug, setMarketSlug] = useState('')
  const [manualTokenId, setManualTokenId] = useState('')
  const [newPrice, setNewPrice] = useState('0.62')
  const [operator, setOperator] = useState('人工')
  const [formError, setFormError] = useState<string | null>(null)

  const ordersQuery = useQuery({
    queryKey: ['orders', { openOnly, conditionId, tokenId, traceId, offset }],
    queryFn: () =>
      adminApi.listOrders({
        limit: 50,
        offset,
        open_only: openOnly,
        condition_id: conditionId || undefined,
        token_id: tokenId || undefined,
        trace_id: traceId || undefined,
      }),
    refetchInterval: openOnly ? 10_000 : 20_000,
  })

  const cancelReplaceMutation = useMutation({
    mutationFn: (payload: { market_slug?: string; token_id?: string; new_price: string; operator: string }) =>
      adminApi.cancelReplaceSell(payload),
    onSuccess: async () => {
      setFormError(null)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['orders'] }),
        queryClient.invalidateQueries({ queryKey: ['markets'] }),
        queryClient.invalidateQueries({ queryKey: ['portfolio'] }),
      ])
    },
  })

  const columns: Array<DataColumn<OrderRecord>> = useMemo(
    () => [
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
      {
        key: 'side',
        header: '方向',
        cell: (row) => (
          <div className="badge-row">
            <StatusPill label={formatOrderSideLabel(row.side)} tone={isSellOrderSide(row.side) ? 'warning' : 'neutral'} />
            <StatusPill label={formatOrderStatusLabel(row.status)} tone={orderTone(row.status)} />
          </div>
        ),
      },
      {
        key: 'price',
        header: '价格 / 数量',
        cell: (row) => (
          <div className="table-primary">
            <strong>{formatDecimal(row.price)}</strong>
            <span>{formatDecimal(row.size_shares)} shares</span>
          </div>
        ),
      },
      {
        key: 'filled',
        header: '已成交',
        align: 'right',
        cell: (row) => formatDecimal(row.filled_shares),
      },
      {
        key: 'trace',
        header: '追踪 ID',
        cell: (row) => row.trace_id ?? '—',
      },
      {
        key: 'updated',
        header: '更新时间',
        cell: (row) => formatDateTime(row.updated_at),
      },
    ],
    [],
  )

  const requestError = cancelReplaceMutation.error ? formatApiError(cancelReplaceMutation.error) : null

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    cancelReplaceMutation.reset()

    const normalizedMarketSlug = marketSlug.trim()
    const normalizedTokenId = manualTokenId.trim()
    const normalizedOperator = operator.trim()
    const numericPrice = Number(newPrice)

    if (!normalizedMarketSlug && !normalizedTokenId) {
      setFormError('市场标识和代币 ID 至少要提供一个，才能定位要处理的卖单。')
      return
    }
    if (!Number.isFinite(numericPrice) || numericPrice <= 0 || numericPrice >= 1) {
      setFormError('new_price 必须是 0 到 1 之间的数字。')
      return
    }
    if (!normalizedOperator) {
      setFormError('操作者不能为空。')
      return
    }

    setFormError(null)
    const targetLabel = normalizedMarketSlug || normalizedTokenId
    const confirmed = window.confirm(`将对 ${targetLabel} 取消现有卖单，并按 ${newPrice.trim()} 重挂卖单，是否继续？`)
    if (!confirmed) {
      return
    }
    cancelReplaceMutation.mutate({
      market_slug: normalizedMarketSlug || undefined,
      token_id: normalizedTokenId || undefined,
      new_price: newPrice.trim(),
      operator: normalizedOperator,
    })
  }

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">订单</p>
          <h1>订单查询与卖单重挂</h1>
          <p>查询当前订单，并在人工确认后执行取消并重挂卖单。</p>
        </div>
      </header>

      <SectionCard title="筛选条件" subtitle="默认先看未完成订单，便于确认当前在场风险。">
        <div className="form-grid form-grid--filters">
          <label>
            <span>仅看未完成订单</span>
            <select value={openOnly ? 'true' : 'false'} onChange={(event) => setOpenOnly(event.target.value === 'true')}>
              <option value="true">是</option>
              <option value="false">否</option>
            </select>
          </label>
          <label>
            <span>条件 ID</span>
            <input value={conditionId} onChange={(event) => setConditionId(event.target.value)} />
          </label>
          <label>
            <span>代币 ID</span>
            <input value={tokenId} onChange={(event) => setTokenId(event.target.value)} />
          </label>
          <label>
            <span>追踪 ID</span>
            <input value={traceId} onChange={(event) => setTraceId(event.target.value)} />
          </label>
        </div>
      </SectionCard>

      <div className="content-grid content-grid--wide-aside">
        <SectionCard
          title="订单列表"
          subtitle={`服务端共 ${ordersQuery.data?.total ?? 0} 条。`}
          actions={
            <div className="inline-actions">
              <button type="button" onClick={() => setOffset((current) => Math.max(0, current - 50))} disabled={offset === 0}>
                上一页
              </button>
              <button
                type="button"
                onClick={() => setOffset((current) => current + 50)}
                disabled={(ordersQuery.data?.items.length ?? 0) < 50}
              >
                下一页
              </button>
            </div>
          }
        >
          <DataTable
            columns={columns}
            rows={ordersQuery.data?.items ?? []}
            rowKey={(row) => row.order_id ?? `${row.condition_id}-${row.token_id}-${row.created_at ?? 'unknown'}`}
            emptyTitle="没有订单"
            emptyDescription="当前筛选下没有匹配结果。"
            onRowClick={(row) => {
              setSelectedOrder(row)
              setMarketSlug(row.market_slug ?? '')
              setManualTokenId(row.token_id)
            }}
            selectedRowKey={selectedOrder?.order_id ?? null}
          />
        </SectionCard>

        <div className="detail-stack">
          <SectionCard title="取消并重挂卖单" subtitle="只用于人工干预已有持仓的卖单。">
            <form className="form-grid" onSubmit={handleSubmit}>
              <label>
                <span>市场标识</span>
                <input value={marketSlug} onChange={(event) => setMarketSlug(event.target.value)} />
              </label>
              <label>
                <span>代币 ID</span>
                <input value={manualTokenId} onChange={(event) => setManualTokenId(event.target.value)} />
              </label>
              <label>
                <span>目标价格</span>
                <input value={newPrice} onChange={(event) => setNewPrice(event.target.value)} />
              </label>
              <label>
                <span>操作者</span>
                <input value={operator} onChange={(event) => setOperator(event.target.value)} />
              </label>
              <button type="submit" disabled={cancelReplaceMutation.isPending}>
                {cancelReplaceMutation.isPending ? '处理中...' : '确认取消并重挂'}
              </button>
            </form>
            {formError ? (
              <ul className="message-list form-feedback">
                <li>
                  <strong>表单校验</strong>
                  <span>{formError}</span>
                </li>
              </ul>
            ) : null}
            {requestError ? (
              <ul className="message-list form-feedback">
                <li>
                  <strong>请求失败</strong>
                  <span>{requestError}</span>
                </li>
              </ul>
            ) : null}
          </SectionCard>

          <SectionCard title="当前选中订单" subtitle="点击左侧订单可快速带入市场标识和 Token ID。">
            <JsonPanel
              value={selectedOrder}
              emptyLabel="尚未选择订单。"
              detailsLabel="查看订单原始数据"
              defaultOpen={Boolean(selectedOrder)}
            />
          </SectionCard>

          <SectionCard title="处理结果" subtitle="展示取消结果、重挂结果和返回摘要。">
            <JsonPanel
              value={cancelReplaceMutation.data}
              emptyLabel="尚未执行取消并重挂。"
              detailsLabel="查看处理结果原始数据"
              summary={
                cancelReplaceMutation.data ? (
                  <div className="detail-list">
                    <div>
                      <dt>处理状态</dt>
                      <dd>{formatOrderStatusLabel(cancelReplaceMutation.data.status)}</dd>
                    </div>
                    <div>
                      <dt>追踪 ID</dt>
                      <dd>{cancelReplaceMutation.data.trace_id}</dd>
                    </div>
                    <div>
                      <dt>操作者</dt>
                      <dd>{cancelReplaceMutation.data.operator ?? '—'}</dd>
                    </div>
                    <div>
                      <dt>结果说明</dt>
                      <dd>{cancelReplaceMutation.data.reason ?? '—'}</dd>
                    </div>
                  </div>
                ) : null
              }
            />
          </SectionCard>
        </div>
      </div>
    </div>
  )
}
