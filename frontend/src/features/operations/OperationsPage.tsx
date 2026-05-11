import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { formatApiError } from '../../core/api/client'
import { adminApi } from '../../core/api/resources'
import type { AllocationRecord } from '../../core/api/types'
import { SectionCard } from '../../shared/ui/SectionCard'
import { DataTable, type DataColumn } from '../../shared/ui/DataTable'
import { JsonPanel } from '../../shared/ui/JsonPanel'
import { MarketExternalLink } from '../../shared/ui/MarketExternalLink'
import { QueryErrorNotice } from '../../shared/ui/QueryErrorNotice'
import { formatDecimal } from '../../shared/utils/format'

export const OperationsPage = () => {
  const queryClient = useQueryClient()
  const [traceId, setTraceId] = useState('')
  const [conditionIdsInput, setConditionIdsInput] = useState('')
  const [pauseReason, setPauseReason] = useState('manual_pause')
  const [pauseOperator, setPauseOperator] = useState('人工')

  const allocationsQuery = useQuery({
    queryKey: ['allocations'],
    queryFn: () =>
      adminApi.listAllocations({
        limit: 100,
        offset: 0,
      }),
  })

  const readyQuery = useQuery({
    queryKey: ['ready'],
    queryFn: () => adminApi.getReady(),
    refetchInterval: 5_000,
  })
  const runtimeQuery = useQuery({
    queryKey: ['runtime'],
    queryFn: () => adminApi.getRuntime(),
    refetchInterval: 10_000,
  })

  const invalidateAfterControl = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['ready'] }),
      queryClient.invalidateQueries({ queryKey: ['runtime'] }),
    ])
  }

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
  const pauseTradingMutation = useMutation({
    mutationFn: (payload: { reason: string; operator: string }) => adminApi.pauseTrading(payload),
    onSuccess: invalidateAfterControl,
  })
  const resumeTradingMutation = useMutation({
    mutationFn: (payload: { operator: string }) => adminApi.resumeTrading(payload),
    onSuccess: invalidateAfterControl,
  })

  const requestError = reconcileMutation.error ? formatApiError(reconcileMutation.error) : null
  const pauseError = pauseTradingMutation.error ? formatApiError(pauseTradingMutation.error) : null
  const resumeError = resumeTradingMutation.error ? formatApiError(resumeTradingMutation.error) : null
  const phase = runtimeQuery.data?.phase ?? readyQuery.data?.phase ?? '未知'
  const manualPauseReason =
    pauseTradingMutation.data?.manual_pause_reason ??
    resumeTradingMutation.data?.manual_pause_reason ??
    null
  const handlePauseTrading = () => {
    pauseTradingMutation.reset()
    const normalizedOperator = pauseOperator.trim() || 'manual'
    const normalizedReason = pauseReason.trim() || 'manual_pause'
    if (!window.confirm(`将以"${normalizedReason}"暂停自动交易，是否继续？`)) return
    pauseTradingMutation.mutate({ reason: normalizedReason, operator: normalizedOperator })
  }
  const handleResumeTrading = () => {
    resumeTradingMutation.reset()
    const normalizedOperator = pauseOperator.trim() || 'manual'
    if (!window.confirm('确认恢复自动交易？')) return
    resumeTradingMutation.mutate({ operator: normalizedOperator })
  }

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
          <h1>人工对账与自动交易控制</h1>
          <p>这里执行受控对账操作、暂停/恢复自动交易，并观察当前预算分配结果。</p>
        </div>
      </header>

      <SectionCard
        title="自动交易开关"
        subtitle={`当前 phase=${phase}${manualPauseReason ? `；manual_pause_reason=${manualPauseReason}` : ''}`}
      >
        <div className="form-grid form-grid--filters">
          <label>
            <span>操作者</span>
            <input value={pauseOperator} onChange={(event) => setPauseOperator(event.target.value)} />
          </label>
          <label>
            <span>暂停原因</span>
            <input value={pauseReason} onChange={(event) => setPauseReason(event.target.value)} />
          </label>
          <div className="inline-actions">
            <button type="button" onClick={handlePauseTrading} disabled={pauseTradingMutation.isPending}>
              {pauseTradingMutation.isPending ? '暂停中...' : '暂停自动交易'}
            </button>
            <button type="button" onClick={handleResumeTrading} disabled={resumeTradingMutation.isPending}>
              {resumeTradingMutation.isPending ? '恢复中...' : '恢复自动交易'}
            </button>
          </div>
        </div>
        {pauseError ? (
          <ul className="message-list form-feedback">
            <li>
              <strong>暂停失败</strong>
              <span>{pauseError}</span>
            </li>
          </ul>
        ) : null}
        {resumeError ? (
          <ul className="message-list form-feedback">
            <li>
              <strong>恢复失败</strong>
              <span>{resumeError}</span>
            </li>
          </ul>
        ) : null}
      </SectionCard>

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
        {allocationsQuery.error ? (
          <QueryErrorNotice title="预算分配加载失败" error={allocationsQuery.error} />
        ) : (
          <DataTable
            columns={allocationColumns}
            rows={allocationsQuery.data?.items ?? []}
            rowKey={(row) => row.idempotency_key ?? `${row.condition_id}-${row.token_id ?? 'unknown'}`}
            emptyTitle="没有分配记录"
            emptyDescription="当前没有可展示的预算分配记录。"
          />
        )}
      </SectionCard>
    </div>
  )
}
