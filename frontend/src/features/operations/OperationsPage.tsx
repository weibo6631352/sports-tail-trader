import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { formatApiError } from '../../core/api/client'
import { adminApi } from '../../core/api/resources'
import type { AllocationRecord } from '../../core/api/types'
import { SectionCard } from '../../shared/ui/SectionCard'
import { DataTable, type DataColumn } from '../../shared/ui/DataTable'
import { JsonPanel } from '../../shared/ui/JsonPanel'
import { MarketExternalLink } from '../../shared/ui/MarketExternalLink'
import { formatDecimal } from '../../shared/utils/format'

export const OperationsPage = () => {
  const queryClient = useQueryClient()
  const [traceId, setTraceId] = useState('')
  const [conditionIdsInput, setConditionIdsInput] = useState('')

  const allocationsQuery = useQuery({
    queryKey: ['allocations'],
    queryFn: () =>
      adminApi.listAllocations({
        limit: 100,
        offset: 0,
      }),
  })

  const reconcileMutation = useMutation({
    mutationFn: (payload: { trace_id?: string; condition_ids: string[] }) => adminApi.reconcile(payload),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['portfolio'] }),
        queryClient.invalidateQueries({ queryKey: ['orders'] }),
        queryClient.invalidateQueries({ queryKey: ['positions'] }),
      ])
    },
  })

  const requestError = reconcileMutation.error ? formatApiError(reconcileMutation.error) : null

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    reconcileMutation.reset()
    reconcileMutation.mutate({
      trace_id: traceId.trim() || undefined,
      condition_ids: conditionIdsInput
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean),
    })
  }

  const allocationColumns: Array<DataColumn<AllocationRecord>> = [
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
          <span>{row.token_id ? `代币 ${row.token_id}` : '—'}</span>
        </div>
      ),
    },
    { key: 'target', header: '目标预算', align: 'right', cell: (row) => formatDecimal(row.target_budget_usdc) },
    { key: 'buy', header: '买入预算', align: 'right', cell: (row) => formatDecimal(row.buy_budget_usdc) },
    {
      key: 'exposure',
      header: '当前敞口',
      align: 'right',
      cell: (row) => formatDecimal(row.current_exposure_usdc),
    },
    { key: 'reason', header: '说明', cell: (row) => row.reason ?? '—' },
  ]

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">操作</p>
          <h1>人工对账与预算分配</h1>
          <p>这里执行受控对账操作，并观察当前预算分配结果。</p>
        </div>
      </header>

      <div className="content-grid content-grid--two">
        <SectionCard title="执行对账" subtitle="支持按追踪 ID 或条件 ID 限定本次对账范围。">
          <form className="form-grid" onSubmit={handleSubmit}>
            <label>
              <span>追踪 ID</span>
              <input value={traceId} onChange={(event) => setTraceId(event.target.value)} />
            </label>
            <label>
              <span>条件 ID 列表</span>
              <textarea
                value={conditionIdsInput}
                onChange={(event) => setConditionIdsInput(event.target.value)}
                rows={4}
                placeholder="用逗号分隔多个条件 ID"
              />
            </label>
            <button type="submit" disabled={reconcileMutation.isPending}>
              {reconcileMutation.isPending ? '执行中...' : '开始对账'}
            </button>
          </form>
          {requestError ? (
            <ul className="message-list form-feedback">
              <li>
                <strong>请求失败</strong>
                <span>{requestError}</span>
              </li>
            </ul>
          ) : null}
        </SectionCard>

        <SectionCard title="对账结果" subtitle="先看摘要，排障时再展开原始返回。">
          <JsonPanel
            value={reconcileMutation.data}
            emptyLabel="尚未执行对账。"
            detailsLabel="查看对账原始数据"
            summary={
              reconcileMutation.data ? (
                <div className="detail-list">
                  <div>
                    <dt>处理状态</dt>
                    <dd>{reconcileMutation.data.status === 'ok' ? '完成' : reconcileMutation.data.status}</dd>
                  </div>
                  <div>
                    <dt>追踪 ID</dt>
                    <dd>{reconcileMutation.data.trace_id}</dd>
                  </div>
                  <div>
                    <dt>结果说明</dt>
                    <dd>{reconcileMutation.data.reason ?? '—'}</dd>
                  </div>
                </div>
              ) : null
            }
          />
        </SectionCard>
      </div>

      <SectionCard title="预算分配" subtitle={`当前 ${allocationsQuery.data?.total ?? 0} 条。`}>
        <DataTable
          columns={allocationColumns}
          rows={allocationsQuery.data?.items ?? []}
          rowKey={(row) => row.idempotency_key ?? `${row.condition_id}-${row.token_id ?? 'unknown'}`}
          emptyTitle="没有分配记录"
          emptyDescription="当前没有可展示的预算分配记录。"
        />
      </SectionCard>
    </div>
  )
}
