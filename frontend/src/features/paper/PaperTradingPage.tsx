import { useMemo, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { formatApiError } from '../../core/api/client'
import { adminApi } from '../../core/api/resources'
import type {
  JsonObject,
  VirtualPaperOpportunityFunnel,
  VirtualPaperOrderRequest,
  VirtualPaperPnl,
  VirtualPaperRejectionSummary,
  VirtualPaperStep,
  VirtualPaperTradeRequest,
} from '../../core/api/types'
import { DataTable, type DataColumn } from '../../shared/ui/DataTable'
import { JsonPanel } from '../../shared/ui/JsonPanel'
import { MarketExternalLink } from '../../shared/ui/MarketExternalLink'
import { SectionCard } from '../../shared/ui/SectionCard'
import { StatusPill } from '../../shared/ui/StatusPill'
import { formatBool, formatDecimal, formatJson } from '../../shared/utils/format'

const statusTone = (status: string): 'success' | 'warning' | 'danger' | 'neutral' => {
  if (status === 'ok' || status === 'success') {
    return 'success'
  }
  if (status === 'no_trade' || status === 'warning') {
    return 'warning'
  }
  if (status === 'failed' || status === 'error') {
    return 'danger'
  }
  return 'neutral'
}

const valueText = (value: unknown): string => {
  if (value === null || value === undefined || value === '') {
    return '—'
  }
  if (typeof value === 'object') {
    return formatJson(value)
  }
  return String(value)
}

const requestPayload = (
  conditionId: string,
  tokenId: string,
  marketSlug: string,
): VirtualPaperTradeRequest => ({
  condition_id: conditionId.trim() || undefined,
  token_id: tokenId.trim() || undefined,
  market_slug: marketSlug.trim() || undefined,
})

interface FunnelRow {
  key: string
  label: string
  count: number | string | null | undefined
  detail: string
}

interface RejectionSummaryRow {
  key: string
  reason: string
  count: number
  detail: string
}

const mapText = (values: Record<string, number> | undefined): string => {
  if (!values || Object.keys(values).length === 0) {
    return '—'
  }
  return Object.entries(values)
    .map(([key, count]) => `${key}:${count}`)
    .join('，')
}

const funnelRows = (funnel: VirtualPaperOpportunityFunnel | undefined): FunnelRow[] => [
  {
    key: 'source',
    label: '源市场',
    count: funnel?.source_market_count,
    detail: `token ${funnel?.source_token_count ?? 0} 个`,
  },
  {
    key: 'orderbook',
    label: '有盘口',
    count: funnel?.orderbook_available_count,
    detail: `已评估 ${funnel?.evaluated_token_count ?? 0} 个 token`,
  },
  {
    key: 'metadata',
    label: '有直播事实',
    count: funnel?.metadata_available_count,
    detail: mapText(funnel?.game_status_counts),
  },
  {
    key: 'plan',
    label: '计划通过',
    count: funnel?.plan_ready_count,
    detail: mapText(funnel?.action_counts),
  },
  {
    key: 'auto',
    label: '自动执行',
    count: funnel?.auto_execute_count,
    detail: mapText(funnel?.market_family_counts),
  },
]

const rejectionSummaryRows = (summary: VirtualPaperRejectionSummary | undefined): RejectionSummaryRow[] =>
  Object.entries(summary?.by_reason ?? {}).map(([reason, count]) => ({
    key: reason,
    reason,
    count,
    detail: `stage ${mapText(summary?.by_stage)} / permission ${mapText(summary?.by_execution_permission)}`,
  }))

const pnlStatusLabel = (paperPnl: VirtualPaperPnl | undefined): string => {
  if (!paperPnl) {
    return '尚未计算'
  }
  if (paperPnl.profitable) {
    return paperPnl.realized ? '已实现盈利' : '预计盈利'
  }
  return paperPnl.realized ? '未盈利' : '未实现盈利'
}

export const PaperTradingPage = () => {
  const [conditionId, setConditionId] = useState('')
  const [tokenId, setTokenId] = useState('')
  const [marketSlug, setMarketSlug] = useState('')

  const mutation = useMutation({
    mutationFn: (payload: VirtualPaperTradeRequest) => adminApi.runVirtualPaperTrade(payload),
  })

  const result = mutation.data
  const selection = result?.selection
  const summary = result?.summary ?? {}
  const opportunityFunnel = result?.opportunity_funnel
  const rejectionSummary = result?.rejection_summary
  const paperPnl = result?.paper_pnl
  const requestError = mutation.error ? formatApiError(mutation.error) : null

  const stepRows = result?.steps ?? []
  const orderRows = result?.order_requests ?? []
  const opportunityRows = funnelRows(opportunityFunnel)
  const rejectionSummaryTableRows = rejectionSummaryRows(rejectionSummary)
  const rejectionRows = result?.rejections ?? []

  const stepColumns: Array<DataColumn<VirtualPaperStep>> = useMemo(
    () => [
      {
        key: 'step',
        header: '步骤',
        cell: (row) => (
          <div className="table-primary">
            <strong>{row.label}</strong>
            <span>{row.key}</span>
          </div>
        ),
      },
      {
        key: 'status',
        header: '状态',
        cell: (row) => <StatusPill label={row.status} tone={statusTone(row.status)} />,
      },
      { key: 'detail', header: '细节', cell: (row) => row.detail ?? '—' },
    ],
    [],
  )

  const orderColumns: Array<DataColumn<VirtualPaperOrderRequest>> = useMemo(
    () => [
      {
        key: 'phase',
        header: '阶段',
        cell: (row) => (
          <div className="table-primary">
            <strong>{row.phase}</strong>
            <span>{row.action}</span>
          </div>
        ),
      },
      {
        key: 'virtual',
        header: '虚拟',
        cell: (row) => <StatusPill label={formatBool(row.virtual)} tone={row.virtual ? 'warning' : 'success'} />,
      },
      { key: 'side', header: '方向', cell: (row) => row.side ?? '—' },
      { key: 'orderType', header: '类型', cell: (row) => row.order_type ?? '—' },
      { key: 'price', header: '价格', align: 'right', cell: (row) => formatDecimal(row.price) },
      { key: 'amount', header: '金额', align: 'right', cell: (row) => formatDecimal(row.amount_usdc) },
      { key: 'shares', header: '份额', align: 'right', cell: (row) => formatDecimal(row.size_shares) },
    ],
    [],
  )

  const funnelColumns: Array<DataColumn<FunnelRow>> = useMemo(
    () => [
      {
        key: 'stage',
        header: '阶段',
        cell: (row) => (
          <div className="table-primary">
            <strong>{row.label}</strong>
            <span>{row.key}</span>
          </div>
        ),
      },
      { key: 'count', header: '数量', align: 'right', cell: (row) => valueText(row.count) },
      { key: 'detail', header: '说明', cell: (row) => row.detail },
    ],
    [],
  )

  const rejectionSummaryColumns: Array<DataColumn<RejectionSummaryRow>> = useMemo(
    () => [
      {
        key: 'reason',
        header: '原因',
        cell: (row) => (
          <div className="table-primary">
            <strong>{row.reason}</strong>
            <span>{row.key}</span>
          </div>
        ),
      },
      { key: 'count', header: '次数', align: 'right', cell: (row) => row.count },
      { key: 'detail', header: '分布', cell: (row) => row.detail },
    ],
    [],
  )

  const rejectionColumns: Array<DataColumn<JsonObject>> = useMemo(
    () => [
      {
        key: 'market',
        header: '市场',
        cell: (row) => (
          <div className="table-primary">
            <strong>{valueText(row.market_slug)}</strong>
            <span>{valueText(row.token_id)}</span>
          </div>
        ),
      },
      { key: 'reason', header: '原因', cell: (row) => valueText(row.reason) },
      { key: 'action', header: '动作', cell: (row) => valueText(row.action) },
      { key: 'permission', header: '权限', cell: (row) => valueText(row.execution_permission) },
    ],
    [],
  )

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    mutation.reset()
    mutation.mutate(requestPayload(conditionId, tokenId, marketSlug))
  }

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">虚拟盘</p>
          <h1>真实数据虚拟提交</h1>
          <p>数据、策略、风控和订单签名使用当前运行态；最后提交付款步骤返回虚拟成交。</p>
        </div>
      </header>

      <div className="content-grid content-grid--two">
        <SectionCard title="运行虚拟盘" subtitle="不填写时自动选择当前第一个 auto_execute 候选。">
          <form className="form-grid" onSubmit={handleSubmit}>
            <label>
              <span>条件 ID</span>
              <input value={conditionId} onChange={(event) => setConditionId(event.target.value)} />
            </label>
            <label>
              <span>Token ID</span>
              <input value={tokenId} onChange={(event) => setTokenId(event.target.value)} />
            </label>
            <label>
              <span>市场 slug</span>
              <input value={marketSlug} onChange={(event) => setMarketSlug(event.target.value)} />
            </label>
            <button type="submit" disabled={mutation.isPending}>
              {mutation.isPending ? '运行中...' : '运行虚拟盘'}
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

        <SectionCard title="执行摘要" subtitle="只看本次虚拟运行，不写入真实订单。">
          <div className="detail-list">
            <div>
              <dt>状态</dt>
              <dd>
                <StatusPill label={result?.status ?? '尚未运行'} tone={statusTone(result?.status ?? '')} />
              </dd>
            </div>
            <div>
              <dt>数据源</dt>
              <dd>{result?.data_source ?? '—'}</dd>
            </div>
            <div>
              <dt>执行边界</dt>
              <dd>{result?.virtual_boundary ?? result?.execution ?? '—'}</dd>
            </div>
            <div>
              <dt>签名来源</dt>
              <dd>{valueText(result?.signing?.source)}</dd>
            </div>
            <div>
              <dt>Trace ID</dt>
              <dd>{result?.trace_id ?? '—'}</dd>
            </div>
            <div>
              <dt>结果说明</dt>
              <dd>{result?.reason ?? valueText(summary.entry_reason)}</dd>
            </div>
            <div>
              <dt>纸面收益</dt>
              <dd>
                <StatusPill
                  label={pnlStatusLabel(paperPnl)}
                  tone={paperPnl?.profitable ? 'success' : result ? 'warning' : 'neutral'}
                />
              </dd>
            </div>
            <div>
              <dt>预计 PnL</dt>
              <dd>{formatDecimal(paperPnl?.projected_gross_pnl_usdc)}</dd>
            </div>
            <div>
              <dt>收益口径</dt>
              <dd>{paperPnl?.basis ?? '—'}</dd>
            </div>
          </div>
        </SectionCard>
      </div>

      <SectionCard title="选中候选" subtitle="来自当前 registry、orderbook 和直播 metadata。">
        <div className="detail-list detail-list--columns">
          <div>
            <dt>市场</dt>
            <dd>
              {selection?.event_slug ? (
                <MarketExternalLink className="market-list__title" eventSlug={selection.event_slug}>
                  {selection.market_slug ?? selection.event_slug}
                </MarketExternalLink>
              ) : (
                selection?.market_slug ?? '—'
              )}
            </dd>
          </div>
          <div>
            <dt>Token</dt>
            <dd>{selection?.token_id ?? '—'}</dd>
          </div>
          <div>
            <dt>动作</dt>
            <dd>{selection?.action ?? '—'}</dd>
          </div>
          <div>
            <dt>权限</dt>
            <dd>{selection?.execution_permission ?? '—'}</dd>
          </div>
          <div>
            <dt>候选原因</dt>
            <dd>{selection?.sports_reason ?? selection?.plan_reason ?? '—'}</dd>
          </div>
          <div>
            <dt>后续卖价</dt>
            <dd>{formatDecimal(summary.follow_up_price as string | number | null | undefined)}</dd>
          </div>
        </div>
      </SectionCard>

      <SectionCard
        title="机会漏斗"
        subtitle={`扫描范围 ${opportunityFunnel?.scan_scope ?? '—'}；${opportunityFunnel?.scan_completed ? '已完成扫描' : '可能提前命中或尚未运行'}`}
      >
        <DataTable
          columns={funnelColumns}
          rows={opportunityRows}
          rowKey={(row) => row.key}
          emptyTitle="没有机会漏斗"
          emptyDescription="运行虚拟盘后展示各阶段掉点。"
        />
      </SectionCard>

      <SectionCard title="链路步骤" subtitle={`当前 ${stepRows.length} 步。`}>
        <DataTable
          columns={stepColumns}
          rows={stepRows}
          rowKey={(row) => row.key}
          emptyTitle="尚未产生步骤"
          emptyDescription="运行虚拟盘后展示链路步骤。"
        />
      </SectionCard>

      <SectionCard title="订单边界" subtitle="sign 为真实运行态签名；submit/cancel/replace 为虚拟。">
        <DataTable
          columns={orderColumns}
          rows={orderRows}
          rowKey={(row) => `${row.phase}-${row.idempotency_key ?? row.side ?? row.action}`}
          emptyTitle="没有订单请求"
          emptyDescription="没有进入订单执行阶段，通常是当前没有可自动执行候选。"
        />
      </SectionCard>

      {rejectionSummaryTableRows.length > 0 ? (
        <SectionCard
          title="拒绝原因聚合"
          subtitle={`累计 ${rejectionSummary?.total ?? 0} 条；${rejectionSummary?.truncated ? '样本已截断' : '样本未截断'}。`}
        >
          <DataTable
            columns={rejectionSummaryColumns}
            rows={rejectionSummaryTableRows}
            rowKey={(row) => row.key}
            emptyTitle="没有拒绝聚合"
            emptyDescription="服务端没有返回拒绝原因聚合。"
          />
        </SectionCard>
      ) : null}

      {rejectionRows.length > 0 ? (
        <SectionCard title="未执行样本" subtitle={`服务端返回 ${rejectionRows.length} 条真实拒绝原因样本。`}>
          <DataTable
            columns={rejectionColumns}
            rows={rejectionRows}
            rowKey={(row) => `${valueText(row.condition_id)}-${valueText(row.token_id)}-${valueText(row.reason)}`}
            emptyTitle="没有拒绝样本"
            emptyDescription="服务端没有返回候选拒绝样本。"
          />
        </SectionCard>
      ) : null}

      <SectionCard title="原始返回" subtitle="排障时查看完整服务端响应。">
        <JsonPanel value={result} emptyLabel="尚未运行虚拟盘。" detailsLabel="查看虚拟盘原始数据" />
      </SectionCard>
    </div>
  )
}
